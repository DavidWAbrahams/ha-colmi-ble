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
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import TYPE_CHECKING, Any, Generic, TypeVar

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
    motion,
    real_time,
    reboot,
    set_time as set_time_mod,
    steps,
)
from .colmi_codec.hr import HeartRateLogParser
from .colmi_codec.steps import SportDetailParser

from .const import (
    LOGGER,
    MOTION_STREAM_SILENCE_TIMEOUT_S,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from homeassistant.core import HomeAssistant


UART_SERVICE_UUID = "6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

# How long to wait for a (possibly multi-packet) reply to a single command.
# The upstream library uses a fixed 2s which is too tight over a Bluetooth
# proxy, so we raise it.
REPLY_TIMEOUT = 10.0

# How long each kind of session will wait for the ring's single GATT link.
#
# A poll in flight must not queue behind an open stream: the ring has no spare
# link, so waiting would only block the coordinator for the whole session. It
# fails fast instead and the caller keeps its last known data.
POLL_LOCK_WAIT_S = 5.0
# The reverse case is common and harmless: toggling the stream on lands within a
# second of the refresh that ran when the previous stream closed, so a poll is
# mid-flight. Those finish in a few seconds, so the stream waits rather than
# reporting a failure the user cannot act on.
#
# The clap listener waits this long too, for the same reason: it is the other
# kind of session that holds the link open indefinitely.
STREAM_LOCK_WAIT_S = 90.0
# Button presses and settings writes come from a person in the UI, so a poll
# already on the air is worth a short wait rather than an instant refusal.
USER_LOCK_WAIT_S = 15.0


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


class ColmiRingApiClientBusyError(ColmiRingApiClientCommunicationError):
    """Exception to indicate the ring's single link is owned by another session.

    Not a failure of the ring: it means we asked for a second simultaneous
    session (poll vs. motion stream), which its firmware cannot serve. Callers
    that have data already should keep it rather than report an error.
    """


class _RingConnection:
    """A single active GATT connection to the ring, managed by HA's BT stack.

    The ring serves exactly one central, and opening a second connection while
    one is live does not queue politely - it drops one of the two links. So a
    connection also holds the client-wide :attr:`_lock` for its whole life,
    which is what keeps a scheduled poll from tearing down an open motion
    stream (and vice versa).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        *,
        lock: asyncio.Lock | None = None,
        lock_wait_s: float = POLL_LOCK_WAIT_S,
        purpose: str = "poll",
    ) -> None:
        """Store the resolved HA instance and the ring MAC address."""
        self._hass = hass
        self._address = address
        self._lock = lock
        self._lock_wait_s = lock_wait_s
        self._purpose = purpose
        self._locked = False
        self._client: BleakClient | None = None
        self._rx_char: BleakGATTCharacteristic | None = None
        # Fresh, stateful parsers per connection so multi-packet logs don't leak.
        self._handlers: dict[int, Callable[[bytearray], Any]] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        # Open-ended streams (the RAW accelerometer) need every packet for a
        # command byte, not one reply per command, so they register here.
        self._listeners: dict[int, list[Callable[[bytearray], None]]] = {}
        # Command bytes already reported as unexpected on this link. An
        # unfamiliar byte is a diagnosis the first time and a flood afterwards,
        # since a stream repeats its frames every second, so each one is named
        # at most once per connection.
        self._logged_unexpected: set[int] = set()
        # Monotonic time the link came up, so a disconnect can be reported with
        # how long it lasted: "the stream died 29 ms after opening" and "the
        # stream died after 20 minutes" are completely different bugs, and
        # without the age the log lines look identical.
        self.linked_at: float | None = None
        # True once *we* started closing the link, so the disconnect callback
        # can say who ended it rather than leaving that to guesswork.
        self.closing = False
        # Set when bleak reports the link dropped, so stream sessions can end
        # instead of waiting out their own timeout.
        self.dead = asyncio.Event()
        # Called with *every* notification on this link, whatever its command
        # byte. Used to account for what a stream session actually received, so
        # a stream on an unexpected command byte is not misread as silence.
        self.observer: Callable[[bytearray], None] | None = None
        # Monotonic timestamp of the most recent motion packet (see
        # ``add_motion_timestamp``); used by the stream watchdog.
        self.last_motion: float | None = None

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

    _LOCK_POLL_S = 0.25

    async def _async_acquire_lock(self) -> None:
        """Claim the ring's single-link lock, waiting up to the caller's budget.

        Polls :meth:`asyncio.Lock.locked` instead of wrapping ``acquire()`` in a
        timeout: cancelling an acquire that is queued on the lock can leave the
        lock held forever, which would silently brick every later session. A
        free lock is claimed synchronously by the next ``acquire()``, so
        checking and then acquiring cannot be slipped between, and a caller that
        must not wait simply runs out its deadline here.
        """
        assert self._lock is not None
        deadline = time.monotonic() + self._lock_wait_s
        while True:
            if not self._lock.locked():
                await self._lock.acquire()
                self._locked = True
                return
            if time.monotonic() >= deadline:
                msg = (
                    f"Ring {self._address} is busy with another session"
                    f" (this one is a {self._purpose})"
                )
                raise ColmiRingApiClientBusyError(msg)
            await asyncio.sleep(self._LOCK_POLL_S)

    def _release_lock(self) -> None:
        if self._locked and self._lock is not None:
            self._locked = False
            self._lock.release()

    async def __aenter__(self) -> _RingConnection:
        """Take the ring's one link, then resolve + connect through HA's stack."""
        if self._lock is not None:
            await self._async_acquire_lock()
        try:
            return await self._async_connect()
        except BaseException:
            self._release_lock()
            raise

    async def _async_connect(self) -> _RingConnection:
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
            self.linked_at = time.monotonic()
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
        try:
            await self._safe_disconnect()
        finally:
            self._release_lock()

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Best-effort: wake any waiting readers so they don't hang.

        The session *purpose*, who closed the link, and the link's age are in
        the message because a bare "disconnected" is useless when several
        sessions share an address: the line that killed the motion stream and
        the line for an ordinary poll finishing look identical, and only the
        age distinguishes "dropped by something else 29 ms after opening" from
        "closed normally at the end of a session".
        """
        age = (
            "unknown age"
            if self.linked_at is None
            else f"after {time.monotonic() - self.linked_at:.3f}s"
        )
        who = "closed by us" if self.closing else "DROPPED BY PEER"
        LOGGER.debug(
            "Colmi ring %s %s session disconnected (%s, %s)",
            self._address,
            self._purpose,
            age,
            who,
        )
        self.dead.set()

    def add_listener(
        self, cmd: int, listener: Callable[[bytearray], None]
    ) -> None:
        """Receive *every* notification whose command byte is ``cmd``."""
        self._listeners.setdefault(cmd, []).append(listener)

    def remove_listener(
        self, cmd: int, listener: Callable[[bytearray], None]
    ) -> None:
        """Undo :meth:`add_listener`; unknown listeners are ignored."""
        listeners = self._listeners.get(cmd)
        if listeners and listener in listeners:
            listeners.remove(listener)

    def touch_motion(self) -> None:
        """Record that motion data just arrived (feeds the stream watchdog)."""
        self.last_motion = time.monotonic()

    async def _safe_disconnect(self) -> None:
        if self._client is None:
            return
        # Set before disconnecting so the callback can tell our own teardown
        # apart from the peer dropping us, which is the distinction that says
        # whether a short-lived stream was closed by us or by the ring.
        self.closing = True
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
        if self.observer is not None:
            # Counted before dispatch so the tally covers bytes no listener on
            # this session was registered for.
            try:
                self.observer(packet)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring packet observer raised")
        # Streams are served first and unconditionally: a command byte may have
        # no one-shot queue at all (RAW motion never does). A listener raising
        # must not tear down the notification callback for everyone else, so
        # each call is fenced.
        listeners = self._listeners.get(cmd, ())
        for listener in list(listeners):
            try:
                listener(packet)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring stream listener raised for cmd %s", cmd)
        handler = self._handlers.get(cmd)
        if handler is None:
            # A frame a stream listener consumed is exactly what that session
            # asked for, so it is not unexpected: at one bundle per second,
            # logging it would bury everything else in the log. Anything else
            # is named once per link. Hex as well as decimal, plus the whole
            # payload: the command bytes this integration knows are a small
            # table, so an unfamiliar one is easier to recognise (and look up)
            # as 0x2F than as 47, and the body is the only evidence available
            # when the ring invents a notification type we have no parser for.
            # The purpose says *which* session received it - the deciding detail
            # when a stream and a poll overlap.
            if not listeners and cmd not in self._logged_unexpected:
                self._logged_unexpected.add(cmd)
                LOGGER.debug(
                    "Unexpected packet type %s (0x%02X) from ring during %s"
                    " session: %s",
                    cmd,
                    cmd,
                    self._purpose,
                    packet.hex(),
                )
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

    async def start_motion_stream(self) -> None:
        """Ask the ring to start streaming RAW accelerometer data."""
        self.touch_motion()
        packet = motion.get_raw_start_packet()
        LOGGER.debug(
            "Colmi ring %s RAW-stream start packet: %s",
            self._address,
            packet.hex(),
        )
        await self._send(packet)

    async def stop_motion_stream(self) -> None:
        """Ask the ring to stop streaming; errors are best-effort ignored.

        Called while tearing a session down, including after the link has
        already dropped, where the write would raise.
        """
        try:
            await self._send(motion.get_raw_stop_packet())
        except ColmiRingApiClientError:
            LOGGER.debug(
                "Could not tell ring %s to stop streaming (already gone?)",
                self._address,
            )

    async def set_camera_gesture(self, enabled: bool) -> bool:
        """Toggle the ring's on-device camera-button (clap) detection.

        Best-effort: this is a write without response and the detection reports
        are notifications rather than a reply, so there is nothing to await
        beyond the write. Returns whether the write reached the ring.

        Only the Clap Detection session uses this. Camera mode does not coexist
        with RAW streaming, so it is deliberately not part of a motion session.
        """
        packet = (
            motion.get_camera_gesture_on_packet()
            if enabled
            else motion.get_camera_gesture_off_packet()
        )
        LOGGER.debug(
            "Colmi ring %s camera-gesture packet (%s): %s",
            self._address,
            "on" if enabled else "off",
            packet.hex(),
        )
        try:
            await self._send(packet)
        except ColmiRingApiClientError:
            LOGGER.debug(
                "Could not set camera gesture %s on ring %s",
                "on" if enabled else "off",
                self._address,
            )
            return False
        return True

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


class _StreamTally:
    """Counts notifications seen by one stream session, by command byte.

    Deliberately tiny and allocation-light: it runs inside the BLE
    notification callback, on every packet of a stream that may run at tens of
    Hz. Its whole purpose is one log line at session close, answering the only
    question that matters when no accelerometer sensor ever updates: did the
    ring send *nothing*, or did it send on a command byte we do not listen to?
    """

    def __init__(self) -> None:
        self.count = 0
        self._by_type: Counter[str] = Counter()

    def add(self, packet: bytearray) -> None:
        """Record one 16-byte notification."""
        self.count += 1
        self._by_type[f"0x{packet[0]:02X}/{packet[1] if len(packet) > 1 else 0}"] += 1

    def count_for(self, cmd: int) -> int:
        """How many notifications carried command byte ``cmd``."""
        prefix = f"0x{cmd:02X}/"
        return sum(
            num for name, num in self._by_type.items() if name.startswith(prefix)
        )

    def describe(self) -> str:
        """Human-readable summary, most frequent type first."""
        return (
            ", ".join(
                f"{name} x{num}" for name, num in self._by_type.most_common(5)
            )
            or "none"
        )


class _LinkSession:
    """One open connection to the ring that its holder may end early.

    Handed out by the session loops in :class:`ColmiRingApiClient`. The holder
    ends it with :meth:`request_stop` (the auto-off timers do this); the
    transport ends it itself if the link drops, or - where that is meaningful -
    if the data stops arriving.
    """

    def __init__(self, conn: _RingConnection) -> None:
        self.conn = conn
        self._stopped = asyncio.Event()
        self.reason: str | None = None

    def request_stop(self, reason: str) -> None:
        """Ask the session to close; the first reason wins."""
        if self.reason is None:
            self.reason = reason
        self._stopped.set()

    async def wait_stopped(self) -> str | None:
        """Block until the session is over, returning the closing reason."""
        await self._stopped.wait()
        return self.reason


class MotionSession(_LinkSession):
    """A live RAW-motion session: one open connection with the stream on.

    Ends by itself if the stream goes silent, since a stream that reports
    nothing is not a working stream.
    """


class ClapSession(_LinkSession):
    """A live on-device clap session: one connection with camera mode armed.

    Silence is the normal state here - the ring reports one packet per clap and
    nothing in between - so this kind of session is never closed for lack of
    traffic, only for a dropped link.
    """


# Either kind of session, so the shared retry loop can be typed against both.
_SessionT = TypeVar("_SessionT", bound=_LinkSession)


@dataclass(frozen=True)
class _SessionKind(Generic[_SessionT]):
    """How one kind of long-lived session is opened, and when to reopen it.

    The motion stream and the clap listener hold the ring's link in the same way
    and want the same retry behaviour; what differs is the opening ritual and
    which closing reasons are worth a reconnect, so those three things travel
    together instead of as loose parameters.
    """

    #: What this kind of session is called in log lines.
    label: str
    #: Returns the context manager that holds the session open.
    opener: Callable[
        [Callable[[bytearray], None]],
        AbstractAsyncContextManager[_SessionT],
    ]
    #: Closing reasons that mean "reconnect and try again"; anything else (a
    #: user turning the switch off, a ring that refuses the enable) ends the loop.
    retry_reasons: tuple[str, ...]


class ColmiRingApiClient:
    """High level client for a Colmi ring, using HA-managed BLE connections."""

    def __init__(self, hass: HomeAssistant, ring_address: str) -> None:
        """Store the HA instance and ring address (no connection yet)."""
        self._hass = hass
        self._ring_address = ring_address
        # Every session to this ring goes through here. The firmware serves one
        # central, so two overlapping connections do not queue - they drop each
        # other's link, which is how a motion stream gets torn off the air
        # seconds after opening.
        self._lock = asyncio.Lock()

    def _connection(self, purpose: str, lock_wait_s: float) -> _RingConnection:
        """A connection that holds the ring's single link for its whole life."""
        return _RingConnection(
            self._hass,
            self._ring_address,
            lock=self._lock,
            lock_wait_s=lock_wait_s,
            purpose=purpose,
        )

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
        async with self._connection("poll", POLL_LOCK_WAIT_S) as conn:
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
        async with self._connection("settings", USER_LOCK_WAIT_S) as conn:
            return await conn.get_hr_log_settings()

    async def async_set_hr_log_settings(
        self, settings: hr_settings.HeartRateLogSettings
    ) -> hr_settings.HeartRateLogSettings:
        """Write the ring's HR logging settings, then read them back to verify.

        Returns the settings as the ring reports them after the write.
        """
        async with self._connection("settings", USER_LOCK_WAIT_S) as conn:
            await conn.set_hr_log_settings(settings)
            await asyncio.sleep(0.5)
            return await conn.get_hr_log_settings()

    async def async_get_battery(self) -> dict[str, Any]:
        """Return battery level + charging state."""
        async with self._connection("battery", USER_LOCK_WAIT_S) as conn:
            info = await conn.get_battery()
            return {
                "battery_level": info.battery_level,
                "charging": info.charging,
            }

    async def async_set_time(self, value: datetime) -> Any:
        """Sync the ring clock (required to interpret logged data)."""
        async with self._connection("set-time", USER_LOCK_WAIT_S) as conn:
            return await conn.set_time(value)

    async def async_blink_twice(self) -> Any:
        """Trigger blink-twice on the ring."""
        async with self._connection("blink", USER_LOCK_WAIT_S) as conn:
            return await conn.blink_twice()

    async def async_reboot(self) -> Any:
        """Reboot the ring."""
        async with self._connection("reboot", USER_LOCK_WAIT_S) as conn:
            return await conn.reboot()

    async def async_take_spo2(self) -> dict[str, Any]:
        """Take an on-demand SpO2 measurement and return the median sample.

        Returns a dict with ``spo2`` (int %) and ``spo2_timestamp`` (aware
        datetime), or ``spo2`` = None if the ring produced no valid reading.
        """
        async with self._connection("spo2", USER_LOCK_WAIT_S) as conn:
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
        async with self._connection("live-hr", USER_LOCK_WAIT_S) as conn:
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

    # How often the stream watchdog inspects link/silence state.
    _STREAM_WATCHDOG_S = 5.0
    # Reconnect delay for a stream that died or could not open: starts short
    # and doubles per consecutive failure. The common causes (a poll still
    # finishing on the one link, a ring that needs a moment to settle) clear in
    # about a second, while a ring that genuinely refuses the stream should be
    # retried politely, not hammered every second for half an hour. The count
    # resets whenever a session opens successfully.
    _STREAM_RETRY_MIN_S = 1.0
    _STREAM_RETRY_MAX_S = 30.0

    @asynccontextmanager
    async def _motion_stream(
        self, on_packet: Callable[[bytearray], None]
    ) -> AsyncIterator[MotionSession]:
        """Hold the connection open with RAW streaming on, until stopped.

        The ring serves a single central, so this connection *is* the link for
        as long as it lives. The coordinator pauses its polls for the duration
        (see ``MotionManager``), but a poll already in flight when the user
        toggles the stream on can only be waited out, hence the generous
        :data:`STREAM_LOCK_WAIT_S`.

        The RAW stream deliberately does *not* also arm the ring's on-device
        clap detector. Measured on an R02 with the ATC RF03 Writer web tool,
        camera mode (``02 04``) runs on its own with no stream and reports one
        ``02 02`` packet per clap, but no clap report was ever seen while RAW
        mode had the accelerometer - the two features read the same sensor and
        one firmware appears to serve only at a time. So arming it here bought
        nothing; it has its own session now, see :meth:`_clap_session`.
        """
        conn = self._connection("motion stream", STREAM_LOCK_WAIT_S)
        session = MotionSession(conn)
        tally = _StreamTally()
        # Observe the whole link, not just the command bytes we listen for: if
        # the firmware streams on a byte other than the guessed 0xA1, the tally
        # still shows packets arriving, which is a different diagnosis and a
        # different fix.
        conn.observer = tally.add
        listener = self._make_packet_listener(session, on_packet)
        async with conn:
            conn.add_listener(motion.CMD_RAW_MODE, listener)
            conn.touch_motion()
            await conn.start_motion_stream()
            watchdog = asyncio.create_task(self._watch_stream(session))
            try:
                # The context manager's ``__aexit__`` runs on every exit path,
                # including an exception raised inside the ``async with`` body.
                yield session
            finally:
                watchdog.cancel()
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
                conn.remove_listener(motion.CMD_RAW_MODE, listener)
                self._log_stream_session(session, tally)
                await conn.stop_motion_stream()

    @asynccontextmanager
    async def _clap_session(
        self, on_packet: Callable[[bytearray], None]
    ) -> AsyncIterator[ClapSession]:
        """Hold the connection open with the ring's own clap detector armed.

        This is the session behind the Clap Detection switch, and it exists
        because a BLE notification only reaches a central that is connected at
        that moment: a clap reported while we are disconnected is simply lost.
        So hearing the ring's clap reports means holding its single link, which
        is also why the switch warns about pausing polling.

        The listener goes in *before* the ``02 04`` write so that a ring which
        does answer the enable cannot have its reply fall on the floor, and the
        matching ``02 06`` goes out on every exit path: the ring remembers the
        setting after we disconnect, so leaving it armed would keep its detector
        running - and costing battery - for as long as the ring is worn.

        No silence timeout, unlike the RAW stream: this session is *expected* to
        hear nothing between claps, which may be hours.
        """
        conn = self._connection("clap detection", STREAM_LOCK_WAIT_S)
        session = ClapSession(conn)
        tally = _StreamTally()
        # Same reasoning as the RAW stream: the tally answers "did the ring send
        # nothing, or did it send on a byte we do not listen to?", and only an
        # observer over the whole link can tell those apart.
        conn.observer = tally.add
        listener = self._make_packet_listener(session, on_packet)
        async with conn:
            conn.add_listener(motion.CMD_CAMERA_GESTURE, listener)
            conn.touch_motion()
            armed = await conn.set_camera_gesture(True)
            if not armed:
                # The write did not reach the ring. Listening anyway would hold
                # the link, freeze polling and report nothing, so end the
                # session and let the retry loop (then the switch) deal with it.
                session.request_stop("unarmed")
            watchdog = asyncio.create_task(self._watch_link(session))
            try:
                yield session
            finally:
                watchdog.cancel()
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
                conn.remove_listener(motion.CMD_CAMERA_GESTURE, listener)
                self._log_clap_session(session, tally, armed)
                await conn.set_camera_gesture(False)

    def _log_clap_session(
        self, session: ClapSession, tally: _StreamTally, armed: bool
    ) -> None:
        """Report what a finished clap session saw, and whether it could work."""
        lived = (
            "an unknown time"
            if session.conn.linked_at is None
            else f"{time.monotonic() - session.conn.linked_at:.1f}s"
        )
        reason = session.reason or "ABORTED (never stopped by anyone)"
        reports = tally.count_for(motion.CMD_CAMERA_GESTURE)
        if not armed:
            LOGGER.info(
                "Colmi ring %s clap detection closed after %s (%s): the ring"
                " never accepted the enable write (02 04), so no clap could be"
                " reported",
                self._ring_address,
                lived,
                reason,
            )
            return
        if reports:
            LOGGER.debug(
                "Colmi ring %s clap detection closed (%s, after %s): %d"
                " camera-gesture notification(s), %d total: %s",
                self._ring_address,
                reason,
                lived,
                reports,
                tally.count,
                tally.describe(),
            )
            return
        # INFO because "the switch is on but nothing ever fires" is the question
        # this answers, and a session that reported nothing after being armed is
        # the evidence that this firmware does not announce the gesture.
        LOGGER.info(
            "Colmi ring %s clap detection closed after %s (%s) with %d"
            " notification(s) and none of them a clap report (0x%02X/02): %s."
            " If you clapped during this session, this firmware probably does"
            " not report the on-device gesture",
            self._ring_address,
            lived,
            reason,
            tally.count,
            motion.CMD_CAMERA_GESTURE,
            tally.describe(),
        )

    @staticmethod
    async def _watch_link(session: _LinkSession) -> None:
        """Close the session when the link drops.

        The clap listener's only failure mode. The RAW stream's silence rule is
        deliberately absent: here, silence means "no claps", not "broken".
        """
        await session.conn.dead.wait()
        session.request_stop("disconnected")

    def _log_stream_session(
        self, session: MotionSession, tally: _StreamTally
    ) -> None:
        """Report what a finished stream session actually saw.

        The question this answers is whether the ring ever answered the RAW
        command, and there are three answers worth telling apart: packets did
        arrive on 0xA1 (the stream works), packets arrived but on other command
        bytes (our guess of ``CMD_RAW_MODE`` is wrong, which is a decoder fix,
        not a connection fix), or nothing arrived at all (the firmware is not
        streaming, or refused and dropped the link). The last two are logged at
        INFO because they are the ones worth reading without DEBUG enabled.
        """
        conn = session.conn
        lived = (
            "an unknown time"
            if conn.linked_at is None
            else f"{time.monotonic() - conn.linked_at:.1f}s"
        )
        # No reason means nobody asked this session to stop, so it did not end on
        # its own: something unwound it (an exception, or a cancelled task). Saying
        # "stopped" there sends the reader looking at the ring for a teardown we
        # caused ourselves, which is exactly how a 2 ms session read as "the ring
        # refuses to stream".
        reason = session.reason or "ABORTED (never stopped by anyone)"
        raw_count = tally.count_for(motion.CMD_RAW_MODE)
        if raw_count:
            LOGGER.debug(
                "Colmi ring %s motion stream closed (%s, after %s): %d"
                " RAW notification(s), %d notification(s) total: %s",
                self._ring_address,
                reason,
                lived,
                raw_count,
                tally.count,
                tally.describe(),
            )
            return
        LOGGER.info(
            "Colmi ring %s motion stream closed after %s (%s) with %d"
            " notification(s) of which none were RAW (0x%02X): %s",
            self._ring_address,
            lived,
            reason,
            tally.count,
            motion.CMD_RAW_MODE,
            tally.describe(),
        )

    @staticmethod
    def _make_packet_listener(
        session: _LinkSession,
        on_packet: Callable[[bytearray], None],
    ) -> Callable[[bytearray], None]:
        """Wrap the caller's callback so the link is marked active on traffic."""

        def listener(packet: bytearray) -> None:
            session.conn.touch_motion()
            on_packet(packet)

        return listener

    async def _watch_stream(self, session: MotionSession) -> None:
        """Close the session if the link drops or the stream stops reporting.

        The disconnect check wakes on the connection's own event rather than
        waiting for the next sweep, so a dropped link is noticed (and retried)
        within a second instead of up to :data:`_STREAM_WATCHDOG_S` later -
        relevant when the ring is dropped by something else on the link.
        """
        while True:
            dead_waiter = asyncio.ensure_future(session.conn.dead.wait())
            try:
                await asyncio.wait({dead_waiter}, timeout=self._STREAM_WATCHDOG_S)
            finally:
                # ``asyncio.wait`` leaves the waiter running on both timeout and
                # cancellation; tidy it up so no task outlives this loop.
                if not dead_waiter.done():
                    dead_waiter.cancel()
            # The event is inspected directly rather than via ``done``: a lost
            # wake-up then costs at most one watchdog sweep, not the session.
            if session.conn.dead.is_set():
                session.request_stop("disconnected")
                return
            last = session.conn.last_motion
            if (
                last is not None
                and time.monotonic() - last > MOTION_STREAM_SILENCE_TIMEOUT_S
            ):
                session.request_stop("silent")
                return

    async def async_stream_motion(
        self,
        on_packet: Callable[[bytearray], None],
        should_run: Callable[[], bool],
        on_session: Callable[[MotionSession], None] | None = None,
    ) -> str | None:
        """Run motion-stream sessions, restarting them while still wanted.

        ``on_packet`` receives every RAW/gesture notification, ``should_run`` is
        polled before each (re)connect and is what ends the loop when the user
        turns the stream off. Returns the reason the stream finally stopped, or
        ``None`` if it never managed to stay open. See :meth:`_run_sessions` for
        when a failure is retried and when it ends the loop.
        """
        return await self._run_sessions(
            _SessionKind(
                label="motion stream",
                opener=self._motion_stream,
                retry_reasons=("disconnected", "silent"),
            ),
            on_packet,
            should_run,
            on_session,
        )

    async def async_listen_claps(
        self,
        on_packet: Callable[[bytearray], None],
        should_run: Callable[[], bool],
        on_session: Callable[[ClapSession], None] | None = None,
    ) -> str | None:
        """Run clap-listener sessions, restarting them while still wanted.

        The motion stream's loop over the clap session. What differs is which
        closing reasons mean "worth another try": silence is this session's
        normal state, so only a dropped link or a ring that refused the ``02 04``
        enable is retried.
        """
        return await self._run_sessions(
            _SessionKind(
                label="clap detection",
                opener=self._clap_session,
                # Silence is normal here, so it is not a reason to reconnect. A
                # ring that refused the enable might just have been busy, and the
                # retry budget bounds how often we ask.
                retry_reasons=("disconnected", "unarmed"),
            ),
            on_packet,
            should_run,
            on_session,
        )

    async def _run_sessions(
        self,
        kind: _SessionKind[_SessionT],
        on_packet: Callable[[bytearray], None],
        should_run: Callable[[], bool],
        on_session: Callable[[_SessionT], None] | None = None,
    ) -> str | None:
        """Hold one kind of session open, reopening it while it is wanted.

        Shared by the motion stream and the clap listener: they differ only in
        how the session is opened and in which closing reasons are worth another
        attempt, but both hold the ring's single link for their whole life, so
        both owe the host the same courtesy about retrying and about knowing when
        to stop.

        A session that drops or cannot open is retried with a doubling delay
        (:data:`_STREAM_RETRY_MIN_S` up to :data:`_STREAM_RETRY_MAX_S`) for as
        long as :data:`_STREAM_GIVEUP_S`, rather than giving up at the first
        failure. Turning the switch off on the very first error made every brief
        hiccup look like a broken feature, and it also threw away the evidence: a
        session that dies on every attempt at 1 s, 2 s, 4 s intervals reports
        that pattern, where a single attempt reports nothing useful. The budget
        is bounded because while it burns the manager still believes this session
        owns the ring and pauses polling; an unrecoverable failure has to end with
        polling resuming and the switch off. A session that opens at all resets
        the budget.

        Failures are reported by return value rather than raised: a session that
        cannot start should let the caller log one line and mark its switch off,
        not raise out of a service call.
        """
        failing_since: float | None = None
        delay = self._STREAM_RETRY_MIN_S
        while should_run():
            # Declared here so a failed ``__aenter__`` cannot leave the previous
            # attempt's session visible to the stability check below.
            session: _SessionT | None = None
            try:
                async with kind.opener(on_packet) as session:
                    self._arm_session(session, on_session)
                    reason = await session.wait_stopped()
                # Only a session that stayed up counts as a success for retry
                # accounting. Resetting the budget the moment a session *opens*
                # means a fault that reproduces every time (a session that dies two
                # milliseconds after opening, say) restarts the budget on every
                # attempt and so retries at the minimum delay forever - while the
                # manager believes this session owns the ring and keeps polling
                # paused. Requiring a real lifetime lets the budget run out and the
                # switch go off.
                if self._session_lived(session) >= self._STREAM_STABLE_S:
                    failing_since = None
                    delay = self._STREAM_RETRY_MIN_S
            except ColmiRingApiClientError as err:
                # WARNING, not DEBUG: this is the line explaining why the
                # stream is not up yet, and it now repeats per attempt on
                # purpose - the attempt count and the error text together are
                # the evidence for *why* (busy vs. out of range vs. no UART
                # service all look different here).
                LOGGER.warning(
                    "Colmi ring %s %s could not start: %s",
                    self._ring_address,
                    kind.label,
                    err,
                )
                if await self._stream_pause(should_run, failing_since, delay):
                    failing_since, delay = self._next_retry(
                        failing_since, delay
                    )
                    continue
                return None
            except asyncio.CancelledError:
                # A session abandoned this way reports "closed (stopped)" with no
                # reason, because cancellation unwinds the ``async with`` without
                # ever setting one. Without this line that case is indistinguishable
                # from the ring refusing to serve us, so say it out loud before
                # handing the cancellation back to whoever started it.
                LOGGER.warning(
                    "Colmi ring %s %s task was cancelled while the session was"
                    " open (reload, shutdown, or a cancelled task)",
                    self._ring_address,
                    kind.label,
                )
                raise
            except Exception:  # noqa: BLE001
                # Anything else that escapes the session - including a fault in the
                # caller's own arming callback, which runs inside the transport -
                # used to propagate silently into the stream task, where it was
                # logged under a different logger than this integration's. That is
                # what "the switch turns itself off a moment after enabling" looked
                # like from the inside: a clean teardown, no reason, no retry, and
                # nothing in this component's log explaining it.
                #
                # The traceback is logged once; a fault that reproduces on every
                # attempt gets one line per retry rather than a stack each time.
                if failing_since is None:
                    LOGGER.exception(
                        "Colmi ring %s %s aborted while the session was open;"
                        " closing it and retrying",
                        self._ring_address,
                        kind.label,
                    )
                else:
                    LOGGER.warning(
                        "Colmi ring %s %s aborted again (see the traceback from"
                        " the first attempt)",
                        self._ring_address,
                        kind.label,
                    )
                if await self._stream_pause(should_run, failing_since, delay):
                    failing_since, delay = self._next_retry(
                        failing_since, delay
                    )
                    continue
                return None
            # A session that died on its own is worth retrying only while the
            # user still wants it; a user-requested stop is final, and so is any
            # reason the caller knows cannot be fixed by reconnecting.
            if reason in kind.retry_reasons and should_run():
                if await self._stream_pause(should_run, failing_since, delay):
                    LOGGER.info(
                        "Colmi ring %s %s ended (%s); reconnecting in %.0fs",
                        self._ring_address,
                        kind.label,
                        reason,
                        delay,
                    )
                    failing_since, delay = self._next_retry(
                        failing_since, delay
                    )
                    continue
                LOGGER.warning(
                    "Colmi ring %s %s keeps ending (%s); giving up after %.0fs"
                    " of retries",
                    self._ring_address,
                    kind.label,
                    reason,
                    self._STREAM_GIVEUP_S,
                )
                return None
            return reason
        return None

    # Total time to keep retrying a stream that never stays open before
    # declaring it unworkable and letting the switch go off.
    _STREAM_GIVEUP_S = 180.0

    # How long a session must survive to count as a working stream, and so to
    # reset the retry budget. Anything shorter is the failure this loop exists to
    # chase, not evidence that the ring streams.
    _STREAM_STABLE_S = 10.0

    @staticmethod
    def _arm_session(
        session: _SessionT,
        on_session: Callable[[_SessionT], None] | None,
    ) -> None:
        """Hand the open session to the caller, and say so if the arming fails.

        The callback arms the session (listeners, timers, the gesture detector). A
        fault there used to propagate straight out of the transport: the session
        closed a couple of milliseconds after opening with no reason set, and the
        traceback was written by Home Assistant's task handler under a
        ``homeassistant.*`` logger - so this integration's log showed a clean,
        unexplained teardown that read exactly like the ring refusing to stream.
        Naming the arming callback here attributes the fault, and re-raising lets
        the retry loop treat it like any other failure: back off, and give up
        inside the budget rather than hold the ring open on a half-armed session.
        """
        if on_session is None:
            return
        try:
            on_session(session)
        except Exception:  # noqa: BLE001
            LOGGER.exception(
                "Colmi ring session arming callback raised; closing the session"
            )
            raise

    @staticmethod
    def _session_lived(session: _LinkSession | None) -> float:
        """Seconds a finished session held the link, or -1 if it never linked."""
        if session is None or session.conn.linked_at is None:
            return -1.0
        return time.monotonic() - session.conn.linked_at

    async def _stream_pause(
        self,
        should_run: Callable[[], bool],
        failing_since: float | None,
        delay: float,
    ) -> bool:
        """Sleep out one retry pause; False means the retry budget is done.

        Single place deciding whether to keep trying, so both failure shapes
        (could not open / died at once) obey the same bound and both stop
        instantly when the user turns the switch off.
        """
        if not should_run():
            return False
        if (
            failing_since is not None
            and time.monotonic() - failing_since >= self._STREAM_GIVEUP_S
        ):
            return False
        await asyncio.sleep(delay)
        return True

    def _next_retry(
        self, failing_since: float | None, delay: float
    ) -> tuple[float, float]:
        """Advance the retry state: when the trouble started, and next delay."""
        return (
            failing_since if failing_since is not None else time.monotonic(),
            min(delay * 2, self._STREAM_RETRY_MAX_S),
        )
