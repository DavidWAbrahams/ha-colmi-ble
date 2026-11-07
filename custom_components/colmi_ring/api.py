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
    stress,
)
from .colmi_codec.hr import HeartRateLogParser
from .colmi_codec.steps import SportDetailParser
from .colmi_codec.stress import StressLogParser

from .const import (
    LOGGER,
    MEASURE_HEART_RATE,
    MEASURE_SPO2,
    MOTION_STREAM_SILENCE_TIMEOUT_S,
    measurement_busy_message,
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

# How long each kind of caller will wait for the ring's shared link.
#
# Under the shared link (see ``SharedLink``) a caller never waits behind a
# whole session, only behind a single command exchange - except against an
# exclusive lease (the RAW motion stream), which refuses transactions outright
# rather than making them queue for its lifetime. So a poll fails fast here and
# the coordinator keeps its last known data.
POLL_LOCK_WAIT_S = 5.0
# An exclusive lease must let the transaction already in flight finish before it
# can take the link to itself. Transactions finish in seconds (their commands
# have their own timeouts), so this generous budget is really for the case
# where the previous lease's teardown is still unwinding.
#
# The wave listener's ``wait_s`` means the same thing nominally, but a shared
# lease never waits for the transaction lock; it only names how long its
# arm/disarm writes (``SharedLink.gate``) will queue behind other traffic.
STREAM_LOCK_WAIT_S = 90.0
# Button presses and settings writes come from a person in the UI, so a poll
# already on the air is worth a short wait rather than an instant refusal.
USER_LOCK_WAIT_S = 15.0
# The shared link is opened on demand and closed this long after its last user
# leaves. Lazy persistence: overlapping callers reuse one connection instead of
# racing to connect one each, while a ring nobody is talking to costs no more
# battery than under the old connect-per-session client (10 s comfortably
# covers a poll's command gaps and a button pressed straight after a refresh).
LINK_IDLE_DISCONNECT_S = 10.0

# The ring records one stress score per 30-minute slot of its (UTC-synced)
# day, so the stress log only needs re-reading when that slot number changes.
STRESS_SLOT_S = 30 * 60


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

    The ring serves exactly one central, so this integration keeps at most one
    connection open at a time and lets every caller share it; that sharing -
    refcounting, the transaction lock, exclusive leases and the idle disconnect
    - lives in :class:`SharedLink`. This class is pure transport: connect,
    dispatch, command. Command handlers are installed per *transaction* (see
    :meth:`begin_transaction`) so stateful multi-packet parsers never leak
    between the users of one long-lived link.
    """

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        """Store the resolved HA instance and the ring MAC address."""
        self._hass = hass
        self._address = address
        # What the link is being used for right now, for log lines only; the
        # shared link restamps it as each user takes the transaction lock.
        self.purpose = "link"
        self._client: BleakClient | None = None
        self._rx_char: BleakGATTCharacteristic | None = None
        # Fresh, stateful parsers per *transaction* so multi-packet logs cannot
        # leak between users of the same link (see begin_transaction).
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

    async def connect(self) -> _RingConnection:
        """Resolve + connect the device through HA's Bluetooth stack."""
        # Resolve to HA's BLEDevice so the connection is routed to the
        # adapter / ESPHome proxy that actually sees the ring.
        target: BLEDevice = await self._async_resolve_device()

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
            await self.close()
            raise
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            await self.close()
            raise ColmiRingApiClientCommunicationError(
                f"Could not connect to Colmi ring {self._address}: {err}"
            ) from err

        return self

    def begin_transaction(self) -> None:
        """Install the command handlers for one transaction on this link.

        Built fresh each time because the log parsers are stateful across the
        multiple packets of one reply: a parser left over from an earlier user
        of the shared link could finish a *later* user's log mid-stream. The
        handlers live only for the transaction that owns the transaction lock,
        and :meth:`end_transaction` removes them, so a reply that arrives late
        (after its command already timed out) is just an unexpected packet
        rather than an answer handed to the wrong caller.
        """
        self._handlers = {
            battery.CMD_BATTERY: battery.parse_battery,
            hr.CMD_READ_HEART_RATE: HeartRateLogParser().parse,
            hr_settings.CMD_HEART_RATE_LOG_SETTINGS: (
                hr_settings.parse_heart_rate_log_settings
            ),
            real_time.CMD_START_REAL_TIME: real_time.parse_real_time_reading,
            steps.CMD_GET_STEP_SOMEDAY: SportDetailParser().parse,
            stress.CMD_READ_STRESS: StressLogParser().parse,
        }
        self._queues = {cmd: asyncio.Queue() for cmd in self._handlers}

    def end_transaction(self) -> None:
        """Tear down the handlers installed by :meth:`begin_transaction`."""
        self._handlers = {}
        self._queues = {}

    def _on_disconnect(self, _client: BleakClient) -> None:
        """Best-effort: wake any waiting readers so they don't hang.

        The link *purpose*, who closed the link, and the link's age are in the
        message because a bare "disconnected" is useless when several kinds of
        caller share an address: the line that killed the motion stream and
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
            "Colmi ring %s link (%s) disconnected (%s, %s)",
            self._address,
            self.purpose,
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

    async def close(self) -> None:
        """Disconnect the GATT link; safe to call twice or on a dead link."""
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
            # The purpose says *what* the link was being used for when it
            # arrived - the deciding detail when a stream and a poll overlap.
            if not listeners and cmd not in self._logged_unexpected:
                self._logged_unexpected.add(cmd)
                LOGGER.debug(
                    "Unexpected packet type %s (0x%02X) from ring during %s:"
                    " %s",
                    cmd,
                    cmd,
                    self.purpose,
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
        """Send a command and await its (final) parsed reply.

        The reply wait races the link's ``dead`` event rather than only the
        timeout: under the shared link the connection can drop *between*
        transactions (the peer ends it, or a lease tears it down), and a caller
        sitting out the full :data:`REPLY_TIMEOUT` for a ring that is already
        gone would make every retry slower than it needs to be.
        """
        await self._send(packet)
        reply = asyncio.ensure_future(self._queues[cmd].get())
        dropped = asyncio.ensure_future(self.dead.wait())
        try:
            done, _ = await asyncio.wait(
                {reply, dropped}, timeout=REPLY_TIMEOUT
            )
            if not done:
                raise ColmiRingApiClientCommunicationError(
                    f"Timed out waiting for command {cmd} from ring"
                    f" {self._address}"
                )
            if dropped in done:
                raise ColmiRingApiClientCommunicationError(
                    f"Link to Colmi ring {self._address} dropped while waiting"
                    f" for command {cmd}"
                )
            return reply.result()
        finally:
            # ``asyncio.wait`` leaves the loser running; both waiters are
            # internal, so cancel whatever is still pending.
            for waiter in (reply, dropped):
                if not waiter.done():
                    waiter.cancel()

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

    async def get_stress_log(self) -> stress.StressLog | stress.NoData:
        """Read today's stress log (one value per 30-minute slot)."""
        return await self._command(
            stress.read_stress_packet(), stress.CMD_READ_STRESS
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
        """Toggle the ring's on-device camera-button (wave) detection.

        Best-effort: this is a write without response and the detection reports
        are notifications rather than a reply, so there is nothing to await
        beyond the write. Returns whether the write reached the ring.

        Only the Wave Detection session uses this. Camera mode does not coexist
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
            # On a shared link the connection can drop mid-measurement; noticing
            # it here beats timing out five more times on a queue nothing will
            # ever feed again.
            if self.dead.is_set():
                raise ColmiRingApiClientCommunicationError(
                    f"Link to Colmi ring {self._address} dropped during"
                    f" real-time {reading_type.name} measurement"
                )
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


class SharedLink:
    """The one GATT link to a ring, shared by every caller in this client.

    The firmware serves a single central, but that is a constraint on the
    *connection*, not on the callers: command replies correlate by command
    byte only, so what actually has to be serialised is one command exchange
    at a time. This class owns that:

    - **Lazy persistence** - the connection is opened on demand by the first
      user and closed :data:`LINK_IDLE_DISCONNECT_S` after the last one
      leaves, so overlapping callers reuse one link while an idle ring costs
      no battery.
    - **Transactions** (:meth:`acquire`) - take the transaction lock, install
      fresh command handlers on the connection, run one logical exchange (a
      poll, a button measurement, a settings write). Callers wait only behind
      another *exchange*, never a whole session.
    - **Exclusive leases** (:meth:`hold` with ``exclusive=True``) - the RAW
      motion stream owns the accelerometer and the ring's attention outright;
      it parks the transaction lock for its life so transactions refuse fast
      instead of queueing behind an open-ended session.
    - **Shared leases** (:meth:`hold` with ``exclusive=False``) - the wave
      listener just needs the connection to stay up so notifications reach
      us; transactions keep interleaving around it, and the lease's own
      arm/disarm writes go through :meth:`gate` so they too are single
      exchanges rather than an unruly send.

    Reconnect supervision deliberately lives *outside* this class (in the
    session retry loop): a link that dies is simply discarded by the next
    :meth:`_ensure_connected`, and whoever was using it fails with a plain
    communication error.
    """

    # Same rationale as the old single-link lock: poll ``locked()`` instead of
    # cancelling a queued acquire, because cancelling an acquire can leave the
    # lock held forever.
    _LOCK_POLL_S = 0.25

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self._hass = hass
        self._address = address
        self._conn: _RingConnection | None = None
        # Guards connect/disconnect/idle-disconnect so two callers never open
        # two links to a one-central ring.
        self._connect_lock = asyncio.Lock()
        # One command exchange at a time (held for a whole lease when that
        # lease is exclusive).
        self._tx_lock = asyncio.Lock()
        self._users = 0
        self._exclusive = False
        self._idle_handle: asyncio.TimerHandle | None = None

    @property
    def exclusive(self) -> bool:
        """True while an exclusive lease (the RAW stream) owns the link."""
        return self._exclusive

    async def _ensure_connected(self, purpose: str) -> _RingConnection:
        """Return a live connection, opening one if needed; never two at once."""
        async with self._connect_lock:
            conn = self._conn
            if conn is not None and conn.dead.is_set():
                # A lease that is still unwinding keeps its own dead object;
                # everyone else gets a fresh link from here on.
                self._conn = None
                await conn.close()
                conn = None
            if conn is None:
                conn = _RingConnection(self._hass, self._address)
                await conn.connect()
                self._conn = conn
            conn.purpose = purpose
            return conn

    def _busy(self, purpose: str, holder: str) -> ColmiRingApiClientBusyError:
        return ColmiRingApiClientBusyError(
            f"Ring {self._address} is busy with {holder}"
            f" (this one is a {purpose})"
        )

    @asynccontextmanager
    async def acquire(
        self, purpose: str, wait_s: float
    ) -> AsyncIterator[_RingConnection]:
        """One serialised command exchange on the shared link.

        The connection is (re)opened outside the wait budget, mirroring the
        old lock-then-connect ordering: connect time is the caller's own cost,
        not contention with another caller.
        """
        self._cancel_idle()
        try:
            conn = await self._ensure_connected(purpose)
            # Counted as a user the moment the link is in hand - with no await
            # between - so an idle disconnect that was already scheduled can
            # never close the connection out from under a caller who just
            # resolved it (the disconnect double-checks the count under the
            # connect lock).
            self._users += 1
        except BaseException:
            self._maybe_idle()
            raise
        have_tx = False
        try:
            deadline = time.monotonic() + wait_s
            while True:
                if conn.dead.is_set():
                    # The link died (or an idle disconnect raced us) between
                    # the connect above and this iteration: get a fresh one,
                    # still within the same wait budget.
                    conn = await self._ensure_connected(purpose)
                if not self._exclusive and not self._tx_lock.locked():
                    await self._tx_lock.acquire()
                    have_tx = True
                    break
                if time.monotonic() >= deadline:
                    raise self._busy(
                        purpose,
                        "an exclusive session"
                        if self._exclusive
                        else "another transaction",
                    )
                await asyncio.sleep(self._LOCK_POLL_S)
            # An exclusive lease could have started between the loop check and
            # the acquire (it only waits on the tx lock, not on this check).
            if self._exclusive:
                raise self._busy(purpose, "an exclusive session")
            conn.purpose = purpose
            conn.begin_transaction()
            yield conn
        finally:
            self._users -= 1
            if have_tx:
                conn.end_transaction()
                self._tx_lock.release()
            self._maybe_idle()

    @asynccontextmanager
    async def hold(
        self, purpose: str, *, exclusive: bool, wait_s: float
    ) -> AsyncIterator[_RingConnection]:
        """Hold the shared link open for a long-lived session.

        An exclusive lease sets the refusal flag *before* claiming the
        transaction lock, so no new transaction can start behind it while it
        waits for the one exchange already in flight to finish (bounded by the
        command timeouts, comfortably inside :data:`STREAM_LOCK_WAIT_S`).

        A shared lease never touches the transaction lock at all: the wave
        listener coexists with polling, and its own writes go through
        :meth:`gate`. ``wait_s`` is then nominally unused, but it names the
        same promise as the exclusive case: this session does not fail just
        because a poll was mid-exchange when the switch flipped.
        """
        self._cancel_idle()
        try:
            conn = await self._ensure_connected(purpose)
        except BaseException:
            self._maybe_idle()
            raise
        if exclusive:
            self._exclusive = True
            try:
                deadline = time.monotonic() + wait_s
                while True:
                    if not self._tx_lock.locked():
                        await self._tx_lock.acquire()
                        break
                    if time.monotonic() >= deadline:
                        raise self._busy(purpose, "another transaction")
                    await asyncio.sleep(self._LOCK_POLL_S)
            except BaseException:
                self._exclusive = False
                self._maybe_idle()
                raise
        self._users += 1
        conn.purpose = purpose
        try:
            yield conn
        finally:
            self._users -= 1
            if exclusive:
                self._tx_lock.release()
                self._exclusive = False
            self._maybe_idle()

    @asynccontextmanager
    async def gate(
        self, purpose: str = "session io", wait_s: float = 30.0
    ) -> AsyncIterator[_RingConnection]:
        """One locked write from inside a shared lease (wave arm/disarm).

        A lease cannot simply ``_send`` whenever it likes: that write would
        interleave with a transaction's packets mid-exchange, and the ring
        correlates replies by command byte alone. Taking the transaction lock
        makes the lease's own traffic just another well-behaved exchange, and
        the modest budget keeps a stuck transaction visible rather than
        silently blocking the session's teardown.
        """
        deadline = time.monotonic() + wait_s
        while True:
            conn = self._conn
            if conn is None or conn.dead.is_set():
                raise ColmiRingApiClientCommunicationError(
                    f"Link to Colmi ring {self._address} is gone"
                    f" (this one is a {purpose})"
                )
            if not self._exclusive and not self._tx_lock.locked():
                await self._tx_lock.acquire()
                break
            if time.monotonic() >= deadline:
                holder = (
                    "an exclusive session" if self._exclusive else "another transaction"
                )
                raise self._busy(purpose, holder)
            await asyncio.sleep(self._LOCK_POLL_S)
        if self._exclusive:
            self._tx_lock.release()
            raise self._busy(purpose, "an exclusive session")
        conn.purpose = purpose
        try:
            yield conn
        finally:
            self._tx_lock.release()

    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _maybe_idle(self) -> None:
        """Arm the idle disconnect if this was the last user of the link."""
        if self._users or self._conn is None or self._idle_handle is not None:
            return
        self._idle_handle = self._hass.loop.call_later(
            LINK_IDLE_DISCONNECT_S, self._async_idle_expired
        )

    def _async_idle_expired(self) -> None:
        self._idle_handle = None
        if self._users == 0:
            # call_later cannot await; the disconnect itself double-checks the
            # user count under the connect lock.
            self._hass.async_create_task(self._idle_disconnect())

    async def _idle_disconnect(self) -> None:
        async with self._connect_lock:
            if self._users or self._conn is None:
                return
            conn, self._conn = self._conn, None
            await conn.close()

    async def close(self) -> None:
        """Tear the link down now (entry unload/reload)."""
        self._cancel_idle()
        async with self._connect_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()


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
        # When *this session* began, not when the link came up: the shared link
        # outlives individual sessions, and the retry loop's stability check is
        # about the session. Measuring it against the link's age would let a
        # lease onto an already-live link look trivially "stable" and reset the
        # retry budget forever on a fault that dies in milliseconds.
        self.started_at = time.monotonic()
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


class WaveSession(_LinkSession):
    """A live on-device wave session: one connection with camera mode armed.

    Silence is the normal state here - the ring reports one packet per wave and
    nothing in between - so this kind of session is never closed for lack of
    traffic, only for a dropped link.
    """


# Either kind of session, so the shared retry loop can be typed against both.
_SessionT = TypeVar("_SessionT", bound=_LinkSession)


@dataclass(frozen=True)
class _SessionKind(Generic[_SessionT]):
    """How one kind of long-lived session is opened, and when to reopen it.

    The motion stream and the wave listener hold the ring's link in the same way
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
        # central, so this client keeps one shared connection and serialises
        # command exchanges on it (see :class:`SharedLink`); long-lived
        # sessions hold it as a lease instead of owning it outright.
        self._link = SharedLink(hass, ring_address)
        # Name of the on-demand measurement in progress, or None. The
        # optical sensor lights different LEDs per reading type, so the ring
        # cannot take two at once; refuse the second press immediately rather
        # than queueing it for fifteen seconds behind the first.
        self._measuring: str | None = None
        # Newest stress sample from the day-history log, and the 30-min slot
        # it was read for. The log only gains a value on slot boundaries, so
        # a fetch is attempted at most once per slot and the cached reading
        # is reported on every poll in between.
        self._stress_reading: dict[str, Any] | None = None
        self._stress_slot: int | None = None

    async def async_close(self) -> None:
        """Tear the shared link down now (entry unload/reload).

        Left alone, the link would drop a few seconds after its last user
        leaves via the idle timer; unloading should not leave a live GATT
        connection owned by nobody in the meantime.
        """
        await self._link.close()

    def _claim_measurement(self, name: str) -> None:
        """Take the ring's one-at-a-time measurement slot or refuse loudly.

        The transaction lock would serialize two measurements anyway, but
        waiting on it means the second press hangs for the first one's whole
        (~30 s) duration and then reports a vague busy timeout. Claiming the
        slot up front fails it instantly with a message that says why.
        """
        if self._measuring is not None:
            raise ColmiRingApiClientBusyError(measurement_busy_message(self._measuring))
        self._measuring = name

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
        async with self._link.acquire("poll", POLL_LOCK_WAIT_S) as conn:
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

            steps_total = distance_total = calories_total = None
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

            # Stress is logged one value per 30-min slot of the ring's day,
            # so the log is re-read only when that slot changes and the
            # cached newest sample is reported in between. This stays after
            # the clock sync above: the stress log carries no timestamps of
            # its own, so its slots are anchored to UTC midnight.
            stress_value, stress_timestamp = await self._async_stress_reading(conn)

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
            "stress": stress_value,
            "stress_timestamp": stress_timestamp,
            "hr_entries": hr_entries,
            "sport_bins": sport_bins,
            # ISO date of a previous-day log query the ring ANSWERED this
            # poll (None when not requested or not answered).
            "prev_day": prev_day,
        }

    async def _async_stress_reading(
        self, conn: _RingConnection
    ) -> tuple[int | None, datetime | None]:
        """Return the newest (stress, timestamp), refetching once per slot.

        The ring fills one stress slot per 30 minutes, so the log is only
        re-read when the current slot number differs from the last one
        attempted; between slots (and after a failed fetch) the cached
        reading is reported as-is.
        """
        slot = int(date_utils.now().timestamp()) // STRESS_SLOT_S
        if slot != self._stress_slot:
            self._stress_slot = slot
            self._stress_reading = await self._async_read_stress(conn)
        reading = self._stress_reading or {}
        value: int | None = reading.get("value")
        timestamp: datetime | None = reading.get("timestamp")
        return value, timestamp

    async def _async_read_stress(
        self, conn: _RingConnection
    ) -> dict[str, Any] | None:
        """Read today's stress log and return its newest sample, or None.

        Rings without the 0x37 log just time out (or answer empty); that
        clears the cached reading, and the next attempt waits for the next
        30-minute slot rather than re-probing every poll.
        """
        try:
            log = await conn.get_stress_log()
        except ColmiRingApiClientError:
            LOGGER.debug(
                "Colmi ring %s did not answer stress query",
                self._ring_address,
            )
            return self._stress_reading  # a failed fetch keeps the last value
        if not isinstance(log, stress.StressLog):
            LOGGER.debug(
                "Colmi ring %s reported an empty stress history",
                self._ring_address,
            )
            return None
        now = date_utils.now()
        # The day's tail slots run past midnight and are zero, but a stale
        # ring clock could put non-zero slots in the "future"; ignore them.
        samples = [s for s in log.samples_with_times() if s[1] <= now]
        if not samples:
            return None
        value, ts = max(samples, key=lambda s: s[1])
        return {"value": value, "timestamp": ts}

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
        async with self._link.acquire("settings", USER_LOCK_WAIT_S) as conn:
            return await conn.get_hr_log_settings()

    async def async_set_hr_log_settings(
        self, settings: hr_settings.HeartRateLogSettings
    ) -> hr_settings.HeartRateLogSettings:
        """Write the ring's HR logging settings, then read them back to verify.

        Returns the settings as the ring reports them after the write.
        """
        async with self._link.acquire("settings", USER_LOCK_WAIT_S) as conn:
            await conn.set_hr_log_settings(settings)
            await asyncio.sleep(0.5)
            return await conn.get_hr_log_settings()

    async def async_get_battery(self) -> dict[str, Any]:
        """Return battery level + charging state."""
        async with self._link.acquire("battery", USER_LOCK_WAIT_S) as conn:
            info = await conn.get_battery()
            return {
                "battery_level": info.battery_level,
                "charging": info.charging,
            }

    async def async_set_time(self, value: datetime) -> Any:
        """Sync the ring clock (required to interpret logged data)."""
        async with self._link.acquire("set-time", USER_LOCK_WAIT_S) as conn:
            return await conn.set_time(value)

    async def async_blink_twice(self) -> Any:
        """Trigger blink-twice on the ring."""
        async with self._link.acquire("blink", USER_LOCK_WAIT_S) as conn:
            return await conn.blink_twice()

    async def async_reboot(self) -> Any:
        """Reboot the ring."""
        async with self._link.acquire("reboot", USER_LOCK_WAIT_S) as conn:
            return await conn.reboot()

    async def async_take_spo2(self) -> dict[str, Any]:
        """Take an on-demand SpO2 measurement and return the median sample.

        Returns a dict with ``spo2`` (int %) and ``spo2_timestamp`` (aware
        datetime), or ``spo2`` = None if the ring produced no valid reading.
        """
        self._claim_measurement(MEASURE_SPO2)
        try:
            async with self._link.acquire("spo2", USER_LOCK_WAIT_S) as conn:
                samples = await conn.poll_real_time_reading(
                    real_time.RealTimeReading.SPO2
                )
        finally:
            self._measuring = None
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
        self._claim_measurement(MEASURE_HEART_RATE)
        try:
            async with self._link.acquire("live-hr", USER_LOCK_WAIT_S) as conn:
                samples = await conn.poll_real_time_reading(
                    real_time.RealTimeReading.HEART_RATE
                )
        finally:
            self._measuring = None
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
        """Hold the link with RAW streaming on, until stopped.

        The RAW stream takes the shared link as an *exclusive* lease: it owns
        the ring's attention and its accelerometer outright, so transactions
        refuse fast for the duration (see ``MotionManager.holds_link``) and
        only the command exchange already in flight is waited out, hence the
        generous :data:`STREAM_LOCK_WAIT_S`. The lease holds the transaction
        lock for its whole life, which is also what lets the start/stop writes
        below go out directly without interleaving with anyone's packets.

        The RAW stream deliberately does *not* also arm the ring's on-device
        wave detector. Measured on an R02 with the ATC RF03 Writer web tool,
        camera mode (``02 04``) runs on its own with no stream and reports one
        ``02 02`` packet per wave, but no wave report was ever seen while RAW
        mode had the accelerometer - the two features read the same sensor and
        one firmware appears to serve only at a time. So arming it here bought
        nothing; it has its own session now, see :meth:`_wave_session`.
        """
        async with self._link.hold(
            "motion stream", exclusive=True, wait_s=STREAM_LOCK_WAIT_S
        ) as conn:
            session = MotionSession(conn)
            tally = _StreamTally()
            # Observe the whole link, not just the command bytes we listen for:
            # if the firmware streams on a byte other than the guessed 0xA1,
            # the tally still shows packets arriving, which is a different
            # diagnosis and a different fix. Cleared on the way out because the
            # shared link outlives this session and must not keep counting for
            # a tally that is already logged.
            conn.observer = tally.add
            listener = self._make_packet_listener(session, on_packet)
            conn.add_listener(motion.CMD_RAW_MODE, listener)
            conn.touch_motion()
            await conn.start_motion_stream()
            watchdog = asyncio.create_task(self._watch_stream(session))
            try:
                # Every exit path (including an exception raised inside the
                # body) runs the teardown below while the lease still holds
                # the transaction lock, so the stop write cannot race a
                # transaction starting right behind our release.
                yield session
            finally:
                watchdog.cancel()
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
                conn.remove_listener(motion.CMD_RAW_MODE, listener)
                conn.observer = None
                self._log_stream_session(session, tally)
                await conn.stop_motion_stream()

    @asynccontextmanager
    async def _wave_session(
        self, on_packet: Callable[[bytearray], None]
    ) -> AsyncIterator[WaveSession]:
        """Hold the link with the ring's own wave detector armed.

        This is the session behind the Wave Detection switch, and it exists
        because a BLE notification only reaches a central that is connected at
        that moment: a wave reported while we are disconnected is simply lost.
        Under the shared link that no longer means *owning* the link, though:
        the lease is shared, so heart-rate polling keeps interleaving with the
        wave listening, and the wave reports themselves need no lock at all -
        they are notifications the listener picks off the link whenever they
        arrive. Only the ``02 04``/``02 06`` writes are locked exchanges (via
        :meth:`SharedLink.gate`) so they cannot interleave with a poll.

        The listener goes in *before* the ``02 04`` write so that a ring which
        does answer the enable cannot have its reply fall on the floor, and the
        matching ``02 06`` goes out on every exit path: the ring remembers the
        setting after we disconnect, so leaving it armed would keep its detector
        running - and costing battery - for as long as the ring is worn.

        No silence timeout, unlike the RAW stream: this session is *expected* to
        hear nothing between waves, which may be hours.
        """
        async with self._link.hold(
            "wave detection", exclusive=False, wait_s=STREAM_LOCK_WAIT_S
        ) as conn:
            session = WaveSession(conn)
            tally = _StreamTally()
            # Same reasoning as the RAW stream: the tally answers "did the ring
            # send nothing, or did it send on a byte we do not listen to?", and
            # only an observer over the whole link can tell those apart. It is
            # cleared on the way out because the shared link outlives this
            # session and must not tally packets for a logged-and-done tally.
            conn.observer = tally.add
            listener = self._make_packet_listener(session, on_packet)
            conn.add_listener(motion.CMD_CAMERA_GESTURE, listener)
            conn.touch_motion()
            async with self._link.gate("wave arm"):
                armed = await conn.set_camera_gesture(True)
            if not armed:
                # The write did not reach the ring. Listening anyway would
                # hold the link open and report nothing, so end the session
                # and let the retry loop (then the switch) deal with it.
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
                conn.observer = None
                self._log_wave_session(session, tally, armed)
                try:
                    async with self._link.gate("wave disarm"):
                        await conn.set_camera_gesture(False)
                except ColmiRingApiClientError:
                    # Best-effort: the link may already be gone (which is why
                    # this session ended), and then there is nothing to disarm.
                    LOGGER.debug(
                        "Could not send the wave-disarm write to ring %s",
                        self._ring_address,
                    )

    def _log_wave_session(
        self, session: WaveSession, tally: _StreamTally, armed: bool
    ) -> None:
        """Report what a finished wave session saw, and whether it could work."""
        lived = f"{time.monotonic() - session.started_at:.1f}s"
        reason = session.reason or "ABORTED (never stopped by anyone)"
        reports = tally.count_for(motion.CMD_CAMERA_GESTURE)
        if not armed:
            LOGGER.info(
                "Colmi ring %s wave detection closed after %s (%s): the ring"
                " never accepted the enable write (02 04), so no wave could be"
                " reported",
                self._ring_address,
                lived,
                reason,
            )
            return
        if reports:
            LOGGER.debug(
                "Colmi ring %s wave detection closed (%s, after %s): %d"
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
            "Colmi ring %s wave detection closed after %s (%s) with %d"
            " notification(s) and none of them a wave report (0x%02X/02): %s."
            " If you waved during this session, this firmware probably does"
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

        The wave listener's only failure mode. The RAW stream's silence rule is
        deliberately absent: here, silence means "no waves", not "broken".
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
        lived = f"{time.monotonic() - session.started_at:.1f}s"
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

    async def async_listen_waves(
        self,
        on_packet: Callable[[bytearray], None],
        should_run: Callable[[], bool],
        on_session: Callable[[WaveSession], None] | None = None,
    ) -> str | None:
        """Run wave-listener sessions, restarting them while still wanted.

        The motion stream's loop over the wave session. What differs is which
        closing reasons mean "worth another try": silence is this session's
        normal state, so only a dropped link or a ring that refused the ``02 04``
        enable is retried.
        """
        return await self._run_sessions(
            _SessionKind(
                label="wave detection",
                opener=self._wave_session,
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

        Shared by the motion stream and the wave listener: they differ only in
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
            # Declared here so a failed opener entry cannot leave the previous
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
        """Seconds a finished session held its lease, or -1 if never opened.

        Measured from the session's own start, not the link's: on a shared link
        a session can fail instantly against an already-live connection, and
        timing it from ``linked_at`` would read as a long, "stable" session and
        silently reset the retry budget on every attempt.
        """
        if session is None:
            return -1.0
        return time.monotonic() - session.started_at

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
