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
import time
from datetime import datetime, timedelta, timezone
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
    real_time,
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


def _latest_sample_is_zero(
    all_slots: list[tuple[int, datetime]],
    entries: list[tuple[int, datetime]],
    filled: int,
    interval_minutes: int,
) -> bool:
    """True if the ring's newest *sample* read 0 bpm (off-finger evidence).

    Slots between samples are also 0 ("no sample taken yet"), so a zero in the
    newest slot only counts when the newest filled slot is clearly newer than
    the newest non-zero sample. A *worn* ring can legitimately show a gap of
    one logging interval plus one 5-min slot: the ring's log index advances
    slot-by-slot (every 5 min) while samples land only once per interval, so
    right after the index steps past a sample the newest filled slot sits
    exactly interval + 5 minutes ahead. Only a gap *beyond* that means the
    ring actually recorded a zero (and then kept advancing), i.e. it saw no
    pulse. Requiring ``> interval + 5`` instead of ``>= interval`` fixes the
    intermittent false "Away" seen at interval boundaries.
    """
    if filled <= 0:
        return False
    if not entries:
        # The ring has slots but nothing but zeros: it is logging and sees no
        # pulse at all.
        return True
    newest_slot_ts = all_slots[filled - 1][1]
    latest_sample_ts = max(ts for _, ts in entries)
    return newest_slot_ts - latest_sample_ts > timedelta(
        minutes=interval_minutes + 5
    )


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

    # Rings advertise roughly once per second, so HA's scanner normally
    # re-lists the device within a couple of poll attempts.
    _RESOLVE_TIMEOUT_S = 10.0
    _RESOLVE_POLL_S = 0.5

    async def _async_resolve_device(self) -> BLEDevice:
        """Resolve the ring to HA's BLEDevice, waiting briefly for a miss.

        ``establish_connection`` requires a real (HA) ``BLEDevice``: passing
        the raw MAC string crashes bleak_retry_connector on BlueZ
        (``'str' object has no attribute 'details'``). So when HA's scanner
        cache does not currently list the ring, wait up to a few seconds for
        one of its advertisements to arrive instead of falling back to the
        address string; if it never shows up, fail with a retryable error
        (UpdateFailed -> next scheduled poll) rather than an AttributeError.
        """
        from homeassistant.components.bluetooth import (  # noqa: PLC0415
            async_ble_device_from_address,
        )

        deadline = time.monotonic() + self._RESOLVE_TIMEOUT_S
        while True:
            device = async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            if device is not None:
                return device
            if time.monotonic() >= deadline:
                msg = (
                    f"Colmi ring {self._address} was not seen by HA bluetooth "
                    f"within {self._RESOLVE_TIMEOUT_S:.0f}s (out of range or "
                    "advertising is off)"
                )
                raise ColmiRingApiClientCommunicationError(msg)
            await asyncio.sleep(self._RESOLVE_POLL_S)

    async def __aenter__(self) -> _RingConnection:
        """Resolve + connect the device through HA's Bluetooth stack."""
        # Resolve to HA's BLEDevice so the connection is routed to the
        # adapter / ESPHome proxy that actually sees the ring.
        target: BLEDevice = await self._async_resolve_device()

        self._handlers = {
            battery.CMD_BATTERY: battery.parse_battery,
            hr.CMD_READ_HEART_RATE: HeartRateLogParser().parse,
            hr_settings.CMD_HEART_RATE_LOG_SETTINGS: (
                hr_settings.parse_heart_rate_log_settings
            ),
            real_time.CMD_START_REAL_TIME: real_time.parse_real_time_reading,
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
        # The ring can drop the GATT link mid-session (walking out of range);
        # bleak then raises BleakError from write_gatt_char. Map it to the
        # retryable client error so the coordinator reports a plain one-line
        # update failure instead of logging a raw traceback.
        try:
            await self._client.write_gatt_char(self._rx_char, packet, response=False)
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            raise ColmiRingApiClientCommunicationError(
                f"Lost connection to Colmi ring {self._address}"
                " (out of range or disconnected)"
            ) from err

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

    async def poll_real_time_reading(
        self, reading_type: real_time.RealTimeReading
    ) -> list[int]:
        """Start an on-demand real-time measurement, collect samples, stop it.

        The ring streams repeated readings (e.g. SPO2 %) after a START packet;
        we gather a handful of non-zero samples then send STOP. Returns the
        collected samples (possibly empty if none were valid). Mirrors the
        upstream driver's poll loop.
        """
        queue = self._queues[real_time.CMD_START_REAL_TIME]
        # Drop any stale samples from a previous measurement.
        while not queue.empty():
            queue.get_nowait()

        await self._send(real_time.get_start_packet(reading_type))

        valid: list[int] = []
        empty_waits = 0
        # Upstream collects 6 valid samples tolerating many empty waits; we cap
        # consecutive empty waits so the button press finishes in bounded time
        # even if the ring never streams (per-sample timeout stays generous for
        # proxy latency).
        while len(valid) < 6 and empty_waits < 5:
            try:
                data: real_time.Reading | real_time.ReadingError = (
                    await asyncio.wait_for(queue.get(), timeout=REPLY_TIMEOUT)
                )
            except asyncio.TimeoutError:
                empty_waits += 1
                continue
            empty_waits = 0
            if isinstance(data, real_time.ReadingError):
                await self._send(real_time.get_stop_packet(reading_type))
                raise ColmiRingApiClientError(
                    f"Ring returned error code {data.code} for real-time "
                    f"reading {reading_type.name}"
                )
            if data.value != 0:
                valid.append(data.value)

        await self._send(real_time.get_stop_packet(reading_type))
        return valid


class ColmiRingApiClient:
    """High level client for a Colmi ring, using HA-managed BLE connections."""

    def __init__(self, hass: HomeAssistant, ring_address: str) -> None:
        """Store the HA instance and ring address (no connection yet)."""
        self._hass = hass
        self._ring_address = ring_address

    async def async_get_data(
        self, include_previous_day: bool = False
    ) -> dict[str, Any]:
        """Return the latest heart rate plus the ring's HR logging settings.

        ``heart_rate`` is ``None`` when the ring has no usable sample for
        today; ``hr_settings`` is ``None`` if the ring did not answer the
        settings query.

        ``include_previous_day`` extends only the backfill time series
        (``hr_entries``/``sport_bins``) with yesterday's UTC-day ring log;
        the live sensor values stay strictly today-based.
        """
        heart_rate: int | None = None
        heart_rate_timestamp: datetime | None = None
        settings: hr_settings.HeartRateLogSettings | None = None
        # Time series for the recorder backfill (see backfill.py):
        # hr_entries: (timestamp, bpm) for each non-zero slot of today's log;
        # sport_bins: (timestamp, steps, meters, kcal) per 15-min bin of today.
        hr_entries: list[tuple[datetime, int]] = []
        sport_bins: list[tuple[datetime, int, int, float]] = []
        # True when the ring's most recently filled HR log slot reads 0 bpm:
        # direct evidence the optical sensor is not on skin right now, even
        # if the newest *non-zero* sample is still within the staleness grace.
        hr_zero_reading = False
        async with _RingConnection(self._hass, self._ring_address) as conn:
            try:
                settings = await conn.get_hr_log_settings()
            except ColmiRingApiClientError:
                LOGGER.debug(
                    "Colmi ring %s did not answer heart-rate settings query",
                    self._ring_address,
                )

            battery_level: int | None = None
            battery_charging: bool | None = None
            try:
                battery_info = await conn.get_battery()
                battery_level = battery_info.battery_level
                battery_charging = battery_info.charging
            except ColmiRingApiClientError:
                LOGGER.debug(
                    "Colmi ring %s did not answer battery query",
                    self._ring_address,
                )

            steps_total: int | None = None
            distance_total: int | None = None
            calories_total: int | None = None
            try:
                # The ring logs sport detail in 15-min bins keyed to its own
                # clock, which we just synced to UTC above, so ask for offset 0
                # ("today"). Sum the bins for the day's total.
                #
                # The raw calorie field counts small calories (0.001 kcal).
                # Verified empirically against the ring's accrual rate (the
                # ring reports ACTIVE calories only, so sleep/sitting accrue
                # almost nothing):
                # - sleeping:      ~940 raw/hr  -> ~0.9 kcal/hr
                # - sitting idle:  ~2,000 raw/hr -> ~2 kcal/hr
                # - dressing + driving kids: ~81,000 raw/hr -> ~81 kcal/hr,
                #   the rate that settles the divisor: /100 would claim an
                #   impossible ~810 kcal/hr for that activity.
                sport = await conn.get_steps(date_utils.start_of_day(date_utils.now()))
                if isinstance(sport, list):
                    steps_total = sum(d.steps for d in sport)
                    distance_total = sum(d.distance for d in sport)
                    calories_total = round(sum(d.calories for d in sport) / 1000)
                    # Keep the raw bins too so the backfill manager can insert
                    # the per-bin history for time spent out of BLE range.
                    sport_bins = sorted(
                        (
                            (d.timestamp, d.steps, d.distance, d.calories / 1000)
                            for d in sport
                        ),
                        key=lambda row: row[0],
                    )
            except ColmiRingApiClientError:
                LOGGER.debug(
                    "Colmi ring %s did not answer steps query",
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
                all_slots = log.heart_rates_with_times()
                entries = [r for r in all_slots if r[0] > 0]
                # Whole-day (timestamp, bpm) series for the backfill manager.
                hr_entries = sorted(
                    ((ts, bpm) for bpm, ts in entries), key=lambda x: x[0]
                )
                # Off-finger evidence only from a *new* zero-bpm sample; index
                # is clamped to now because the ring can transmit its whole day
                # of slots and the parser's is-today cleanup zeroes (but does
                # not shrink) the future ones.
                interval = settings.interval if settings else 15
                # Slots started so far today (slot k covers minutes 5k..5k+4).
                # minutes_so_far() is "minutes + 1", so subtract it back before
                # dividing: otherwise the clamp can admit a not-yet-started
                # slot, inflating the zero-evidence gap by a spurious 5 min.
                slots_now = (
                    date_utils.minutes_so_far(date_utils.now()) - 1
                ) // 5 + 1
                filled = min(max(log.index, 0), len(all_slots), slots_now)
                hr_zero_reading = _latest_sample_is_zero(
                    all_slots, entries, filled, interval
                )
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

            # Yesterday's ring logs extend the backfill time series only;
            # every live sensor value above stays strictly today-based.
            # ``prev_day`` reports which earlier day was answered this poll so
            # the backfill manager can stop asking for it (see async_sync).
            hr_entries, sport_bins, prev_day = (
                await self._async_extend_previous_day(
                    conn, include_previous_day, hr_entries, sport_bins
                )
            )

        return {
            "heart_rate": heart_rate,
            "heart_rate_timestamp": heart_rate_timestamp,
            "hr_zero_reading": hr_zero_reading,
            "hr_settings": settings,
            "battery_level": battery_level,
            "battery_charging": battery_charging,
            "steps": steps_total,
            "distance": distance_total,
            "calories": calories_total,
            "hr_entries": hr_entries,
            "sport_bins": sport_bins,
            # ISO date of a previous-day log query the ring ANSWERED this
            # poll (None when not requested or not answered).
            "prev_day": prev_day,
        }

    async def _async_extend_previous_day(
        self,
        conn: _RingConnection,
        include_previous_day: bool,
        hr_entries: list[tuple[datetime, int]],
        sport_bins: list[tuple[datetime, int, int, float]],
    ) -> tuple[
        list[tuple[datetime, int]],
        list[tuple[datetime, int, int, float]],
        str | None,
    ]:
        """Merge yesterday's UTC-day ring logs into the backfill series.

        The ring keys its logs to UTC days, so a western user's daytime
        samples land mostly in the *previous* UTC day's log, which today's
        query alone can never reach. Requested only until the backfill
        manager records that day as fetched-and-imported (see
        BackfillManager.async_needs_day / async_sync retry_day).
        Best-effort: a query the ring refuses (or times out on) just leaves
        that metric's series untouched, never a failed poll.

        Returns the merged series plus the ISO date of a day whose HR log the
        ring actually ANSWERED (None when not requested or answered with no
        data), so the manager only stops asking once, not on refusals.
        """
        if not include_previous_day:
            return hr_entries, sport_bins, None
        prev_day = date_utils.start_of_day(date_utils.now()) - timedelta(days=1)
        answered: str | None = None
        try:
            log = await conn.get_heart_rate_log(prev_day)
            if isinstance(log, hr.HeartRateLog):
                answered = prev_day.date().isoformat()
                hr_entries = hr_entries + [
                    (ts, bpm)
                    for bpm, ts in log.heart_rates_with_times()
                    if bpm > 0
                ]
        except ColmiRingApiClientError:
            LOGGER.debug(
                "Colmi ring %s did not answer previous-day heart-rate query",
                self._ring_address,
            )
        try:
            sport = await conn.get_steps(prev_day)
            if isinstance(sport, list):
                sport_bins = sport_bins + [
                    (d.timestamp, d.steps, d.distance, d.calories / 1000)
                    for d in sport
                ]
        except ColmiRingApiClientError:
            LOGGER.debug(
                "Colmi ring %s did not answer previous-day steps query",
                self._ring_address,
            )
        return (
            sorted(hr_entries, key=lambda row: row[0]),
            sorted(sport_bins, key=lambda row: row[0]),
            answered,
        )

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

    async def async_take_spo2(self) -> dict[str, Any]:
        """Take an on-demand SpO2 measurement and return the median sample.

        Returns a dict with ``spo2`` (int %) and ``spo2_timestamp`` (aware
        datetime), or ``spo2`` = None if the ring produced no valid reading.
        """
        async with _RingConnection(self._hass, self._ring_address) as conn:
            samples = await conn.poll_real_time_reading(
                real_time.RealTimeReading.SPO2
            )
        if not samples:
            return {"spo2": None, "spo2_timestamp": None}
        samples.sort()
        median = samples[len(samples) // 2]
        return {
            "spo2": median,
            "spo2_timestamp": datetime.now(tz=timezone.utc),
        }

    async def async_take_heart_rate(self) -> dict[str, Any]:
        """Take an on-demand real-time heart-rate measurement.

        Returns a dict with ``heart_rate`` (int bpm) and ``heart_rate_timestamp``
        (aware datetime), or ``heart_rate`` = None if the ring produced no
        valid reading.
        """
        async with _RingConnection(self._hass, self._ring_address) as conn:
            samples = await conn.poll_real_time_reading(
                real_time.RealTimeReading.HEART_RATE
            )
        if not samples:
            return {"heart_rate": None, "heart_rate_timestamp": None}
        samples.sort()
        median = samples[len(samples) // 2]
        return {
            "heart_rate": median,
            "heart_rate_timestamp": datetime.now(tz=timezone.utc),
        }
