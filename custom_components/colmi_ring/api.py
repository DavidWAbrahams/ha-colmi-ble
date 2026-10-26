"""Colmi Ring API client.

This integration uses only the *pure* packet codecs (vendored into the local
``colmi_codec`` subpackage, originally from the ``colmi_r02_client`` project) and
owns its own Bluetooth transport. The transport is wired through Home Assistant's Bluetooth
stack (``homeassistant.components.bluetooth`` + ``bleak_retry_connector``) so
connections use the device HA actually discovered and are routed to whichever
adapter (local radio or ESPHome Bluetooth proxy) saw it, with retry handling.

Using HA's stack rather than the library's own ``Client`` (which hard-codes a
host-local ``bleak.BleakClient(address)``) is correct either way: on a host with
a local radio ``async_ble_device_from_address`` returns the locally-discovered
device and ``establish_connection`` performs the same host-local connect the
library does -- but it additionally works when the ring is reached via a proxy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from .colmi_codec import (
    battery,
    blink_twice,
    date_utils,
    hr,
    hr_settings,
    reboot,
    set_time as set_time_mod,
    steps,
)
from .colmi_codec.hr import HeartRateLogParser
from .colmi_codec.steps import SportDetailParser

from .const import LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant


UART_SERVICE_UUID = "6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

# How long to wait for a (possibly multi-packet) reply to a single command.
# The upstream library uses a fixed 2s which is too tight over a Bluetooth
# proxy, so we raise it.
REPLY_TIMEOUT = 10.0


class ColmiRingApiClientError(Exception):
    """Exception to indicate a general API error."""


class ColmiRingApiClientCommunicationError(ColmiRingApiClientError):
    """Exception to indicate a communication error."""


class ColmiRingApiClientAuthenticationError(ColmiRingApiClientError):
    """Exception to indicate an authentication error."""


class _RingConnection:
    """A single active GATT connection to the ring, managed by HA's BT stack."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        """Store the resolved HA instance and the ring MAC address."""
        self._hass = hass
        self._address = address
        self._client: BleakClient | None = None
        self._rx_char: BleakGATTCharacteristic | None = None
        # Fresh, stateful parsers per connection so multi-packet logs don't leak.
        self._handlers: dict[int, Callable[[bytearray], Any]] = {}
        self._queues: dict[int, asyncio.Queue] = {}

    async def __aenter__(self) -> _RingConnection:
        """Resolve + connect the device through HA's Bluetooth stack."""
        # Resolve to HA's BLEDevice so the connection is routed to the adapter /
        # ESPHome proxy that actually sees the ring. Falls back to the raw
        # address only if HA has never seen the device (e.g. a local radio).
        from homeassistant.components.bluetooth import (  # noqa: PLC0415
            async_ble_device_from_address,
        )

        device: BLEDevice | None = async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if device is None:
            LOGGER.warning(
                "Colmi ring %s not found by HA bluetooth; falling back to raw "
                "address (only works with a local radio)",
                self._address,
            )
        target = device if device is not None else self._address

        self._handlers = {
            battery.CMD_BATTERY: battery.parse_battery,
            hr.CMD_READ_HEART_RATE: HeartRateLogParser().parse,
            hr_settings.CMD_HEART_RATE_LOG_SETTINGS: (
                hr_settings.parse_heart_rate_log_settings
            ),
            steps.CMD_GET_STEP_SOMEDAY: SportDetailParser().parse,
        }
        self._queues = {cmd: asyncio.Queue() for cmd in self._handlers}

        try:
            self._client = await establish_connection(
                BleakClient,
                target,
                name="Colmi Ring",
                disconnected_callback=self._on_disconnect,
            )

            service = self._client.services.get_service(UART_SERVICE_UUID)
            if service is None:
                msg = f"Ring {self._address} does not expose the Colmi UART service"
                raise ColmiRingApiClientCommunicationError(msg)
            rx_char = service.get_characteristic(UART_RX_CHAR_UUID)
            if rx_char is None:
                msg = f"Ring {self._address} is missing the UART RX characteristic"
                raise ColmiRingApiClientCommunicationError(msg)
            self._rx_char = rx_char

            await self._client.start_notify(UART_TX_CHAR_UUID, self._handle_tx)
        except ColmiRingApiClientError:
            await self._safe_disconnect()
            raise
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            await self._safe_disconnect()
            raise ColmiRingApiClientCommunicationError(
                f"Could not connect to Colmi ring {self._address}: {err}"
            ) from err

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Disconnect and release the GATT connection."""
        await self._safe_disconnect()

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Best-effort: wake any waiting readers so they don't hang."""
        LOGGER.debug("Colmi ring %s disconnected", self._address)

    async def _safe_disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except (BleakError, asyncio.TimeoutError, OSError):
            LOGGER.debug("Ignoring disconnect error for %s", self._address)

    def _handle_tx(self, _: BleakGATTCharacteristic, packet: bytearray) -> None:
        """Bleak notification callback: dispatch a 16-byte packet to its parser."""
        if len(packet) != 16:
            LOGGER.debug("Ignoring packet of unexpected length: %s", packet)
            return
        cmd = packet[0]
        handler = self._handlers.get(cmd)
        if handler is None:
            LOGGER.debug("Unexpected packet type %s from ring", cmd)
            return
        result = handler(packet)
        if result is not None:
            self._queues[cmd].put_nowait(result)

    async def _send(self, packet: bytearray) -> None:
        assert self._client is not None and self._rx_char is not None
        await self._client.write_gatt_char(self._rx_char, packet, response=False)

    async def _command(self, packet: bytearray, cmd: int) -> Any:
        """Send a command and await its (final) parsed reply."""
        await self._send(packet)
        try:
            return await asyncio.wait_for(
                self._queues[cmd].get(), timeout=REPLY_TIMEOUT
            )
        except asyncio.TimeoutError as err:
            raise ColmiRingApiClientCommunicationError(
                f"Timed out waiting for command {cmd} from ring {self._address}"
            ) from err

    async def get_battery(self) -> battery.BatteryInfo:
        """Read the battery level + charging state."""
        return await self._command(battery.BATTERY_PACKET, battery.CMD_BATTERY)

    async def get_heart_rate_log(
        self, target: datetime | None = None
    ) -> hr.HeartRateLog | hr.NoData:
        """Read the heart-rate log for one day (defaults to today)."""
        if target is None:
            target = date_utils.start_of_day(date_utils.now())
        return await self._command(
            hr.read_heart_rate_packet(target), hr.CMD_READ_HEART_RATE
        )

    async def get_steps(
        self, target: datetime, today: datetime | None = None
    ) -> list[steps.SportDetail] | steps.NoData:
        """Read the step/sport log for ``target`` (day offset relative to ring clock)."""
        if today is None:
            today = datetime.now(tz=timezone.utc)
        if target.tzinfo != timezone.utc:
            target = target.astimezone(tz=timezone.utc)
        days = (today.date() - target.date()).days
        return await self._command(
            steps.read_steps_packet(days), steps.CMD_GET_STEP_SOMEDAY
        )

    async def set_time(self, ts: datetime) -> None:
        """Write the current time to the ring (no reply expected)."""
        await self._send(set_time_mod.set_time_packet(ts))

    async def blink_twice(self) -> None:
        """Trigger the ring's blink-twice locate action (no reply expected)."""
        await self._send(blink_twice.BLINK_TWICE_PACKET)

    async def reboot(self) -> None:
        """Reboot the ring (no reply expected)."""
        await self._send(reboot.REBOOT_PACKET)

    async def get_hr_log_settings(self) -> hr_settings.HeartRateLogSettings:
        """Read the ring's heart-rate logging settings (enabled + interval)."""
        return await self._command(
            hr_settings.READ_HEART_RATE_LOG_SETTINGS_PACKET,
            hr_settings.CMD_HEART_RATE_LOG_SETTINGS,
        )

    async def set_hr_log_settings(
        self, settings: hr_settings.HeartRateLogSettings
    ) -> None:
        """Write the ring's heart-rate logging settings (no reply expected)."""
        await self._send(hr_settings.hr_log_settings_packet(settings))


class ColmiRingApiClient:
    """High level client for a Colmi ring, using HA-managed BLE connections."""

    def __init__(self, hass: HomeAssistant, ring_address: str) -> None:
        """Store the HA instance and ring address (no connection yet)."""
        self._hass = hass
        self._ring_address = ring_address

    async def async_get_data(self) -> dict[str, Any]:
        """Return the latest heart rate plus the ring's HR logging settings.

        ``heart_rate`` is ``None`` when the ring has no usable sample for
        today; ``hr_settings`` is ``None`` if the ring did not answer the
        settings query.
        """
        heart_rate: int | None = None
        heart_rate_timestamp: datetime | None = None
        settings: hr_settings.HeartRateLogSettings | None = None
        async with _RingConnection(self._hass, self._ring_address) as conn:
            try:
                settings = await conn.get_hr_log_settings()
            except ColmiRingApiClientError:
                LOGGER.debug(
                    "Colmi ring %s did not answer heart-rate settings query",
                    self._ring_address,
                )

            # The ring indexes its heart-rate log by its *own* internal clock,
            # and we request "today" in UTC. If the ring's clock is wrong (e.g.
            # never synced since pairing) the requested day does not exist on the
            # ring, so it returns no data. Sync the ring clock to UTC first so the
            # day we ask for matches the day the ring logs to (the upstream
            # library likewise requires set-time before reading logs).
            await conn.set_time(date_utils.now())
            await asyncio.sleep(0.5)
            log = await conn.get_heart_rate_log()
            if not isinstance(log, hr.HeartRateLog):
                LOGGER.info(
                    "Colmi ring %s returned no heart-rate log (NoData) for "
                    "requested UTC day %s - ring clock/day mismatch?",
                    self._ring_address,
                    date_utils.start_of_day(date_utils.now()).isoformat(),
                )
            else:
                # Heart rate is sampled at the ring's logging interval but logged
                # in 5-min slots, so most slots are 0 and must be filtered out
                # before picking the latest.
                entries = [r for r in log.heart_rates_with_times() if r[0] > 0]
                if not entries:
                    LOGGER.info(
                        "Colmi ring %s returned a heart-rate log but every slot "
                        "was 0/empty (log day=%s, raw slots=%d, filled=%d) - ring "
                        "has not recorded HR today or its clock is off",
                        self._ring_address,
                        log.timestamp.isoformat(),
                        len(log.heart_rates),
                        log.index,
                    )
                else:
                    heart_rate, heart_rate_timestamp = max(
                        entries, key=lambda x: x[1]
                    )

        return {
            "heart_rate": heart_rate,
            "heart_rate_timestamp": heart_rate_timestamp,
            "hr_settings": settings,
        }

    async def async_get_hr_settings(self) -> hr_settings.HeartRateLogSettings:
        """Read the ring's heart-rate logging settings."""
        async with _RingConnection(self._hass, self._ring_address) as conn:
            return await conn.get_hr_log_settings()

    async def async_set_hr_log_settings(
        self, settings: hr_settings.HeartRateLogSettings
    ) -> hr_settings.HeartRateLogSettings:
        """Write the ring's HR logging settings, then read them back to verify.

        Returns the settings as the ring reports them after the write.
        """
        async with _RingConnection(self._hass, self._ring_address) as conn:
            await conn.set_hr_log_settings(settings)
            await asyncio.sleep(0.5)
            return await conn.get_hr_log_settings()

    async def async_get_battery(self) -> dict[str, Any]:
        """Return battery level + charging state."""
        async with _RingConnection(self._hass, self._ring_address) as conn:
            info = await conn.get_battery()
            return {
                "battery_level": info.battery_level,
                "charging": info.charging,
            }

    async def async_set_time(self, value: datetime) -> Any:
        """Sync the ring clock (required to interpret logged data)."""
        async with _RingConnection(self._hass, self._ring_address) as conn:
            return await conn.set_time(value)

    async def async_blink_twice(self) -> Any:
        """Trigger blink-twice on the ring."""
        async with _RingConnection(self._hass, self._ring_address) as conn:
            return await conn.blink_twice()

    async def async_reboot(self) -> Any:
        """Reboot the ring."""
        async with _RingConnection(self._hass, self._ring_address) as conn:
            return await conn.reboot()
