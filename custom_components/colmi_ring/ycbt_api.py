"""
Yucheng YCBT ring API client (R20/R99 protocol family).

Sibling of :mod:`.api` for the second device family this integration serves.
Like the Colmi client it owns its GATT transport through Home Assistant's
Bluetooth stack (``async_ble_device_from_address`` + ``bleak_retry_connector``)
so a ring reached via an ESPHome Bluetooth proxy works the same as one on the
local radio, and it raises the *same* exception classes so the coordinator and
config flow handle both families uniformly.

Protocol specifics this client encodes (implemented in ``colmi_codec.ycbt``):

* Frames are variable-length ``[group][key][len][payload][crc]`` on the
  vendor service, not Colmi's fixed 16-byte packets. Writes must be
  write-without-response; the firmware is sensitive to it.
* Replies echo the request opcode, so commands wait on their own opcode.
* The ring serves one central: every connection holds the client-wide lock
  for its whole life, same as the Colmi client.
* History is push-per-ACK: a block is ACKed only when it carries records
  newer than our per-stream high-water mark, which parks the backlog at our
  position without ever deleting from the ring (delete opcodes are never
  sent, so the OEM companion app keeps working).
* Record timestamps are "local wall clock stored as UTC", so parsing needs
  the host tz offset; the ring clock is therefore synced to *local* time.

The dict returned by :meth:`YcbtRingApiClient.async_get_data` has exactly the
keys of the Colmi client's. ``hr_settings`` is always ``None`` (the family has
no ring-side HR logging settings) and ``hr_zero_reading`` is always ``False``
(no slot-log heuristic exists for this protocol).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.helpers.storage import Store

from .api import (
    POLL_LOCK_WAIT_S,
    REPLY_TIMEOUT,
    USER_LOCK_WAIT_S,
    ColmiRingApiClientAuthenticationError,
    ColmiRingApiClientBusyError,
    ColmiRingApiClientCommunicationError,
    ColmiRingApiClientError,
)
from .colmi_codec import date_utils
from .colmi_codec.ycbt import constants as yc
from .colmi_codec.ycbt import packet as yc_pkt
from .colmi_codec.ycbt import records as yc_rec
from .const import (
    DEFAULT_YCBT_MONITOR_INTERVAL,
    LOGGER,
    clamp_ycbt_monitor_interval,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice
    from homeassistant.core import HomeAssistant

# How long an on-demand measurement runs before we stop the ring and give up.
# The PPG needs a ~10-20 s warm-up before pushes become plausible.
MEASURE_SECONDS = 30.0
# SpO2 and temperature sessions on the R99 compute their result late: a
# field capture showed the 0x060D progress counter still climbing when a
# 30 s window closed. Both long-measurement types use this window.
LONG_MEASURE_SECONDS = 60.0
# Once a live value has appeared, keep collecting this long (the median needs
# a handful of samples, and pushing longer only delays the button press
# landing).
MEASURE_SETTLE_S = 12.0
# How often to poll 0x0211 ("IS") mid-session: the SDK's real-time
# getRealBloodOxygen equivalent, answered on the command characteristic.
SPO2_POLL_S = 3.0
# In the R99 0x060D progress payload, byte 21 is an elapsed-seconds
# counter (verified in a field capture); skip it when scanning for values.
PROGRESS_SECONDS_INDEX = 21
# Physiologically plausible SpO2 percent range, used when scanning the
# undocumented 0x060D / 0x0211 / 0x040E payloads for a value byte.
SPO2_PLAUSIBLE = range(50, 101)
# Plausible whole-degC skin-temperature range, the equivalent guard when
# scanning live/progress/completion payloads for a temperature byte. The
# int-part byte of an ``int, frac`` encoding (as in AllRecord) lands here.
TEMP_PLAUSIBLE = range(25, 43)
# Silence that ends a history drain: the firmware only sends the next block
# in reply to an ACK, so a missing block within this window means the stream
# has no more data for us.
DRAIN_BLOCK_TIMEOUT_S = 12.0
# Hard block cap per stream per poll, so a firmware that keeps replaying the
# same block cannot spin a poll forever.
DRAIN_MAX_BLOCKS = 400
# A high-water mark further ahead than this is clock corruption (ring or
# host), not the future: reset it so history can flow again.
HWM_FUTURE_SKEW_S = 5 * 60

_HWM_STORE_VERSION = 1


def _now_local() -> datetime:
    """Aware local-now: its wall-clock fields are what the ring should store."""
    return datetime.now().astimezone()


def _tz_offset_sec() -> int:
    offset = _now_local().utcoffset()
    return int(offset.total_seconds()) if offset else 0


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _median(samples: list[int]) -> int:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


async def _async_measure_window(
    samples: list[int],
    window_s: float,
    completed: Callable[[], bool],
    poll: Callable[[], Any] | None = None,
) -> None:
    """
    Watch a started measurement until its value settles or time is up.

    Stops early once ``completed()`` reports the ring finished AND a sample
    arrived, or once ``MEASURE_SETTLE_S`` has passed since the first sample
    (the median needs a handful of values). ``poll`` runs every
    ``SPO2_POLL_S`` while waiting - the R99 ``0x0211`` real-time query.
    """
    deadline = time.monotonic() + window_s
    next_poll = time.monotonic() + SPO2_POLL_S
    first_at: float | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        if samples and first_at is None:
            first_at = time.monotonic()
        if completed() and samples:
            break
        if first_at is not None and time.monotonic() - first_at >= MEASURE_SETTLE_S:
            break
        if poll is not None and time.monotonic() >= next_poll:
            next_poll = time.monotonic() + SPO2_POLL_S
            await poll()


def _measure_extra_samples(
    opcode: int,
    packet: yc_pkt.YcbtPacket,
    context: str,
    plausible: range,
    *,
    fallback: bool = False,
) -> list[int]:
    """
    Measurement values carried by R99 frames other than the live push.

    The R99 answers a manual session with per-second ``0x060D`` progress
    frames (byte 21 is an elapsed-seconds counter; its value fields stay
    zero until the ring computes a reading), answers the ``0x0211`` ("IS")
    real-time query on the command characteristic, and may carry the final
    value in its ``0x040E``/``0x0413`` completion payload. None of these
    layouts is documented, so only bytes in ``plausible`` (SpO2 or skin
    temperature) are accepted, and undocumented nonzero progress bytes are
    logged for later diagnosis. ``context`` identifies the ring and
    measurement type in the logs. Returns ``[]`` for any other opcode.
    """
    if not packet.payload:
        return []
    if opcode == yc.REAL_UPLOAD_MEASURE_PROGRESS:
        nonzero = [
            (idx, byte)
            for idx, byte in enumerate(packet.payload)
            if byte and idx != PROGRESS_SECONDS_INDEX
        ]
        if nonzero:
            LOGGER.debug(
                "Yucheng ring %s progress nonzero bytes: %s",
                context,
                [(idx, f"0x{byte:02X}") for idx, byte in nonzero],
            )
        return [byte for _, byte in nonzero if byte in plausible]
    if opcode == yc.GET_REAL_BLOOD_OXYGEN:
        values = [
            byte
            for byte in packet.payload.removeprefix(yc.MAGIC_GET_REAL_BLOOD_OXYGEN)
            if byte in plausible
        ]
        if values:
            LOGGER.debug(
                "Yucheng ring %s poll reply payload=%s values=%s",
                context,
                packet.payload.hex(),
                values,
            )
        return values
    if opcode in (yc.MEASUREMENT_COMPLETE, yc.MEASUREMENT_STATUS):
        return [byte for byte in packet.payload if byte in plausible]
    if fallback:
        # Temperature's live push opcode is undocumented, so when asked we
        # treat any other frame as a candidate and scan it too.
        values = [byte for byte in packet.payload if byte in plausible]
        if values:
            LOGGER.debug(
                "Yucheng ring %s frame 0x%04X payload=%s values=%s",
                context,
                opcode,
                packet.payload.hex(),
                values,
            )
        return values
    return []


def hrs_bpm(data: bytes) -> int | None:
    """
    Decode a standard 0x2A37 Heart Rate Measurement value.

    Honours the sensor-contact bits: a frame that says "contact supported,
    not worn" is ignored rather than reported as a plausible-looking number.
    """
    if not data:
        return None
    flags = data[0]
    if (flags & 0x06) == 0x04:
        return None
    if flags & 0x01:
        return int.from_bytes(data[1:3], "little") if len(data) >= 3 else None
    return data[1] if len(data) >= 2 else None


class _YcbtConnection:
    """
    One GATT link to a YCBT ring, with the client-wide lock held for life.

    Same one-central constraint as the Colmi rings: a second connection while
    one is live drops a link instead of queueing, so acquiring this object IS
    claiming the ring.
    """

    _RESOLVE_TIMEOUT_S = 10.0
    _RESOLVE_POLL_S = 0.5
    _LOCK_POLL_S = 0.25

    def __init__(  # noqa: PLR0913 - one bundle of per-connection knobs
        self,
        hass: HomeAssistant,
        address: str,
        *,
        lock: asyncio.Lock | None = None,
        lock_wait_s: float = POLL_LOCK_WAIT_S,
        purpose: str = "poll",
        monitor_interval_minutes: int = DEFAULT_YCBT_MONITOR_INTERVAL,
        on_seen: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._address = address
        # Invoked on every inbound notification (see the Colmi client's
        # _RingConnection for the contract); feeds last-seen tracking.
        self._on_seen = on_seen
        self._lock = lock
        self._lock_wait_s = lock_wait_s
        self._purpose = purpose
        # Background monitoring interval the init burst (re-)applies, chosen
        # by the owner from the entry options; see async_set_monitor_interval.
        self._monitor_interval = clamp_ycbt_monitor_interval(monitor_interval_minutes)
        self._locked = False
        self._client: BleakClient | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._notify_char_uuids: list[str] = []
        # One queue per inbound opcode, fed by the notification callback.
        # Lazily created so unsolicited pushes (real-time uploads) are not
        # dropped just because no command asked for that opcode.
        self._queues: dict[int, asyncio.Queue] = {}
        self._live_listeners: list[Callable[[int, yc_pkt.YcbtPacket], None]] = []
        self._hrs_listeners: list[Callable[[int], None]] = []
        self._hrs_uuid: str | None = None
        # Opcodes the firmware answered with its single-byte 0xFC refusal on
        # the current init burst. A 0xFC is per-command, not a session
        # verdict, so the burst rethinks itself only when *nothing* was
        # answered.
        self.refusals: set[int] = set()

    # ------------------------------------------------------- lifecycle

    async def __aenter__(self) -> Self:
        if self._lock is not None:
            await self._async_acquire_lock()
        try:
            return await self._async_connect()
        except BaseException:
            self._release_lock()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            await self._safe_disconnect()
        finally:
            self._release_lock()

    async def _async_acquire_lock(self) -> None:
        """
        Claim the ring's single-link lock within the caller's budget.

        Polls ``locked()`` rather than cancelling ``acquire()``: a cancelled
        acquire can leave the lock held forever (see the Colmi client).
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

    async def _async_resolve_device(self) -> BLEDevice:
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
                    f"Yucheng ring {self._address} was not seen by HA bluetooth"
                    f" within {self._RESOLVE_TIMEOUT_S:.0f}s (out of range or "
                    "advertising is off)"
                )
                raise ColmiRingApiClientCommunicationError(msg)
            await asyncio.sleep(self._RESOLVE_POLL_S)

    async def _async_connect(self) -> _YcbtConnection:
        target: BLEDevice = await self._async_resolve_device()
        try:
            self._client = await establish_connection(
                BleakClient,
                target,
                name="Yucheng Ring",
                disconnected_callback=lambda _c: None,
            )
            self._pick_characteristics(self._client)
            for char_uuid in self._notify_char_uuids:
                await self._client.start_notify(char_uuid, self._handle_notify)
        except ColmiRingApiClientError:
            await self._safe_disconnect()
            raise
        except (TimeoutError, BleakError, OSError) as err:
            await self._safe_disconnect()
            raise ColmiRingApiClientCommunicationError(
                f"Could not connect to Yucheng ring {self._address}: {err}"
            ) from err
        return self

    async def _safe_disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except (TimeoutError, BleakError, OSError):
            LOGGER.debug("Ignoring disconnect error for %s", self._address)

    # ------------------------------------------------------- GATT map

    def _pick_characteristics(self, client: BleakClient) -> None:
        """
        Locate write/notify characteristics, tolerating the R99's FEE7 alias.

        Some firmwares expose the documented ``be940000`` service; others
        (R99) alias the same characteristics under ``0000fee7``. Either way
        the write characteristic must support write-without-response, and
        every notifiable characteristic in the vendor service is subscribed
        because push frames (uploads) may arrive on a different one than
        command replies.
        """
        vendor_uuids = {
            yc.UUID_SERVICE.lower(),
            "0000fee7-0000-1000-8000-00805f9b34fb",
        }
        write_char: BleakGATTCharacteristic | None = None
        notify_uuids: list[str] = []
        for service in client.services:
            if service.uuid.lower() not in vendor_uuids:
                continue
            chars = list(service.characteristics)
            for char in chars:
                if write_char is None and "write-without-response" in char.properties:
                    write_char = char
                if "notify" in char.properties or "indicate" in char.properties:
                    notify_uuids.append(char.uuid)
            # Prefer the documented be940001 write characteristic.
            for char in chars:
                if yc.UUID_CHAR_WRITE in char.uuid.lower():
                    write_char = char
                    break
        if write_char is None or not notify_uuids:
            msg = (
                f"Ring {self._address} does not expose a Yucheng vendor"
                " write/notify characteristic pair"
            )
            raise ColmiRingApiClientCommunicationError(msg)
        self._write_char = write_char
        self._notify_char_uuids = notify_uuids

        for service in client.services:
            if service.uuid.lower() == yc.UUID_HRS_SERVICE:
                for char in service.characteristics:
                    if char.uuid.lower() == yc.UUID_HRS_CHAR:
                        self._hrs_uuid = char.uuid
                        # Subscribed on demand by the measurement flow;
                        # finding it here is enough to know it exists.
                        return

    @property
    def has_hrs(self) -> bool:
        return self._hrs_uuid is not None

    async def start_hrs(self) -> None:
        if self._hrs_uuid is not None and self._client is not None:
            try:
                await self._client.start_notify(self._hrs_uuid, self._handle_hrs)
            except (TimeoutError, BleakError, OSError):
                LOGGER.debug(
                    "Yucheng ring %s refused HRS subscribe; vendor pushes only",
                    self._address,
                )
                self._hrs_uuid = None

    async def stop_hrs(self) -> None:
        if self._hrs_uuid is not None and self._client is not None:
            with contextlib.suppress(TimeoutError, BleakError, OSError):
                await self._client.stop_notify(self._hrs_uuid)

    # ------------------------------------------------------- dispatch

    def _note_seen(self) -> None:
        """Any inbound notification proves the ring is present."""
        if self._on_seen is None:
            return
        try:
            self._on_seen()
        except Exception:  # noqa: BLE001
            LOGGER.exception("Yucheng ring on-seen callback raised")

    def _handle_hrs(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._note_seen()
        LOGGER.debug("Yucheng ring %s HRS raw: %s", self._address, bytes(data).hex())
        bpm = hrs_bpm(bytes(data))
        if bpm:
            for listener in list(self._hrs_listeners):
                try:
                    listener(bpm)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("Yucheng ring HRS listener raised")

    def _handle_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._note_seen()
        raw = bytes(data)
        packet = yc_pkt.decode(raw)
        if packet is None:
            # Pad so the length-field bytes exist even on a runt frame.
            head = raw.ljust(4, b"\x00")
            LOGGER.debug(
                "Ignoring undecodable notification from %s on char %s:"
                " %s (len_field=0x%02X%02X crc_ok=%s)",
                self._address,
                _char.uuid,
                raw.hex(),
                head[2],
                head[3],
                yc_pkt.crc_ok(raw),
            )
            return
        LOGGER.debug(
            "Yucheng ring %s frame 0x%04X on char %s: payload=%s crc_ok=%s",
            self._address,
            packet.data_type,
            _char.uuid,
            packet.payload.hex(),
            yc_pkt.crc_ok(raw),
        )
        if yc_pkt.is_rejection(packet):
            self.refusals.add(packet.data_type)
            LOGGER.debug(
                "Yucheng ring %s answered 0xFC (refused) on opcode 0x%04X",
                self._address,
                packet.data_type,
            )
            # Not queued: callers already treat "no reply" as missing data,
            # and async_init reads this set to tell "refused" from "silent".
            return
        self._queues.setdefault(packet.data_type, asyncio.Queue()).put_nowait(packet)
        for listener in list(self._live_listeners):
            try:
                listener(packet.data_type, packet)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Yucheng ring frame listener raised")

    def add_live_listener(
        self, listener: Callable[[int, yc_pkt.YcbtPacket], None]
    ) -> None:
        self._live_listeners.append(listener)

    def remove_live_listener(
        self, listener: Callable[[int, yc_pkt.YcbtPacket], None]
    ) -> None:
        if listener in self._live_listeners:
            self._live_listeners.remove(listener)

    def add_hrs_listener(self, listener: Callable[[int], None]) -> None:
        self._hrs_listeners.append(listener)

    def remove_hrs_listener(self, listener: Callable[[int], None]) -> None:
        if listener in self._hrs_listeners:
            self._hrs_listeners.remove(listener)

    # ------------------------------------------------------- commands

    async def send(self, packet: yc_pkt.YcbtPacket) -> None:
        """Write one frame. Must stay write-without-response (firmware bug)."""
        assert self._client is not None and self._write_char is not None
        try:
            await self._client.write_gatt_char(
                self._write_char, packet.encode(), response=False
            )
        except (TimeoutError, BleakError, OSError) as err:
            raise ColmiRingApiClientCommunicationError(
                f"Lost connection to Yucheng ring {self._address}"
                " (out of range or disconnected)"
            ) from err

    async def get_reply(
        self, opcode: int, timeout_s: float = REPLY_TIMEOUT
    ) -> yc_pkt.YcbtPacket | None:
        """Next already-received or future frame with ``opcode``, or None."""
        queue = self._queues.setdefault(opcode, asyncio.Queue())
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout_s)
        except TimeoutError:
            return None

    def clear_replies(self) -> None:
        """Drop queued frames so a command never reads a stale reply."""
        for queue in self._queues.values():
            while not queue.empty():
                queue.get_nowait()

    async def async_init(self) -> tuple[set[int], set[int]]:
        """
        Bind/time/enable burst mirroring the companion app's sync entry.

        Returns ``(answered, refused)``: the opcodes the ring replied to,
        and the opcodes it answered with 0xFC, on the last burst. Field
        rings show 0xFC is a *per-command* refusal - a variant lacking a
        feature declines just that opcode while battery, steps and name
        still land - so the burst counts as accepted as soon as anything
        is answered. The one case that replays the burst, led by the SDK's
        user-bind write (0x0109), is a ring that refused commands and
        answered none of them: what a genuine "not bound" lockout looks
        like.
        """
        refused: set[int] = set()
        answered: set[int] = set()
        for attempt in (1, 2):
            self.refusals = set()
            self.clear_replies()
            packets = [
                yc_pkt.setting_time(_now_local()),
                # The four all-day monitors. Each takes {enable, interval};
                # these are what make the ring record anything between
                # syncs, so all four ride the one configured interval.
                # (HRV, 0x0145, is skipped: its documented payload tail is
                # unverified.)
                yc_pkt.set_monitor_interval(self._monitor_interval),
                yc_pkt.set_blood_pressure_monitor(self._monitor_interval),
                yc_pkt.set_temperature_monitor(self._monitor_interval),
                yc_pkt.set_blood_oxygen_monitor(self._monitor_interval),
                yc_pkt.enable_health_sensors(True),
                yc_pkt.get_device_name(),
                yc_pkt.get_device_info(),
                yc_pkt.get_power_statistics(),
                yc_pkt.get_now_step(),
            ]
            if attempt == 2:
                packets.insert(0, yc_pkt.set_user_bind())
            for packet in packets:
                await self.send(packet)
                await asyncio.sleep(0.15)
            # Let the replies land: battery/steps come off these queues right
            # after init, and over a proxy the last frame can take a second.
            await asyncio.sleep(1.5)
            refused = set(self.refusals)
            answered = {
                opcode for opcode, queue in self._queues.items() if not queue.empty()
            }
            if answered or not refused:
                break
            LOGGER.debug(
                "Yucheng ring %s refused every init command on attempt %d"
                " (0xFC); replaying once behind a bind write",
                self._address,
                attempt,
            )
        if refused and answered:
            LOGGER.debug(
                "Yucheng ring %s init accepted; refused opcodes: %s",
                self._address,
                ", ".join(f"0x{opcode:04X}" for opcode in sorted(refused)),
            )
        return answered, refused


class YcbtRingApiClient:
    """High level client for a Yucheng YCBT ring (R20/R99 family)."""

    def __init__(
        self,
        hass: HomeAssistant,
        ring_address: str,
        monitor_interval_minutes: int = DEFAULT_YCBT_MONITOR_INTERVAL,
        on_seen: Callable[[], None] | None = None,
    ) -> None:
        """
        Store the HA instance and ring address (no connection yet).

        ``on_seen`` reaches every inbound notification of every connection
        (see the Colmi client's _RingConnection for the contract); the setup
        passes the coordinator's throttled last-seen tracker.
        """
        self._hass = hass
        self._ring_address = ring_address
        self._on_seen = on_seen
        # The number entity's current value, re-applied on every connect.
        self._monitor_interval = clamp_ycbt_monitor_interval(monitor_interval_minutes)
        # Every session to this ring goes through here (one central).
        self._lock = asyncio.Lock()
        # Per-history-stream high-water marks (unix ms), persisted so a
        # restart does not re-download (and re-ACK-forward) old backlog.
        self._hwm: dict[str, int] = {}
        self._hwm_loaded = False
        # Last known skin temperature: {"skin_temperature": float,
        # "skin_temperature_timestamp": datetime} or None. Persisted across
        # polls because the history stream delivers each record exactly once
        # (watermark-deduped), so without this the sensor would flap to
        # unknown on every poll that drains no new records.
        self._skin_reading: dict[str, Any] | None = None
        # Last known HRV (ms): same persist-across-polls reason as
        # _skin_reading; rides in the same all-metrics records.
        self._hrv_reading: dict[str, Any] | None = None
        self._store = Store(
            hass,
            _HWM_STORE_VERSION,
            f"colmi_ring_ycbt_hwm_{ring_address.lower()}",
        )

    def _connection(self, purpose: str, lock_wait_s: float) -> _YcbtConnection:
        """A connection that holds the ring's single link for its whole life."""
        return _YcbtConnection(
            self._hass,
            self._ring_address,
            lock=self._lock,
            lock_wait_s=lock_wait_s,
            purpose=purpose,
            monitor_interval_minutes=self._monitor_interval,
            on_seen=self._on_seen,
        )

    # ------------------------------------------------------- watermarks

    async def _async_load_hwm(self) -> None:
        if self._hwm_loaded:
            return
        self._hwm_loaded = True
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001
            LOGGER.exception("Could not load YCBT sync watermarks")
            return
        if isinstance(stored, dict):
            self._hwm = {
                str(k): int(v) for k, v in stored.items() if isinstance(v, (int, float))
            }

    def _hwm_for(self, stream_key: str) -> int:
        """Stored watermark, reset if it claims a >5-min future timestamp."""
        hwm = int(self._hwm.get(stream_key, 0))
        future_limit_ms = int(time.time() * 1000) + HWM_FUTURE_SKEW_S * 1000
        if hwm > future_limit_ms:
            LOGGER.info(
                "Yucheng ring %s %s watermark %s is in the future; resetting",
                self._ring_address,
                stream_key,
                hwm,
            )
            hwm = 0
            self._hwm[stream_key] = 0
        return hwm

    async def _async_save_hwm(self) -> None:
        try:
            await self._store.async_save(dict(self._hwm))
        except Exception:  # noqa: BLE001
            LOGGER.exception("Could not persist YCBT sync watermarks")

    # ------------------------------------------------------- drain

    async def _async_drain(
        self,
        conn: _YcbtConnection,
        request_opcode: int,
        parse: Callable[..., list],
        stream_key: str,
    ) -> list[Any]:
        """
        Drain one history stream, ACKing only blocks with new records.

        The firmware re-sends its oldest unACKed block on each request/ACK,
        so ACKing a block whose records are all at-or-before our watermark
        would advance the ring's cursor past data we may still want. Instead
        the first stale block stops the ACKs and the stream parks in place;
        nothing is ever deleted from the ring.
        """
        stream = yc.STREAM_FOR_REQUEST[request_opcode]
        tz = _tz_offset_sec()
        hwm = self._hwm_for(stream_key)
        fresh_rows: list[Any] = []
        parked = False

        await conn.send(yc_pkt.health_history(request_opcode))
        for _ in range(DRAIN_MAX_BLOCKS):
            packet = await conn.get_reply(stream, timeout_s=DRAIN_BLOCK_TIMEOUT_S)
            if packet is None:
                break
            rows = parse(packet.payload, tz_offset_sec=tz)
            fresh = [r for r in rows if r.timestamp_ms > hwm]
            if not fresh:
                # Fully-synced tail block: stop without ACKing.
                parked = True
                break
            fresh_rows.extend(fresh)
            block_hwm = max(r.timestamp_ms for r in rows)
            hwm = max(hwm, block_hwm)
            await conn.send(yc_pkt.history_ack(stream, len(packet.payload) + 6))
            # A ring with a big backlog answers back-to-back; the tiny gap
            # keeps the single write characteristic from queueing.
            await asyncio.sleep(0.05)
        self._hwm[stream_key] = hwm
        if parked:
            LOGGER.debug(
                "Yucheng ring %s %s stream parked at %d",
                self._ring_address,
                stream_key,
                hwm,
            )
        return fresh_rows

    # ------------------------------------------------------- public API

    async def async_get_data(
        self, include_previous_day: bool = False
    ) -> dict[str, Any]:
        """
        Return the ring's latest readings, dict-identical to the Colmi API.

        Live values (``heart_rate``/``steps``) come from the ring's *now*
        commands and today's records; the backfill series (``hr_entries`` /
        ``sport_bins``) come from whatever the history streams newly deliver
        this poll (watermark-deduped), which naturally spans days when the
        ring was out of range.
        """
        await self._async_load_hwm()

        heart_rate: int | None = None
        heart_rate_timestamp: datetime | None = None
        battery_level: int | None = None
        # The power statistics frame carries no charge state, and this
        # family offers no charging binary sensor.
        battery_charging: bool | None = None
        steps_total: int | None = None
        distance_total: int | None = None
        calories_total: int | None = None
        hr_entries: list[tuple[datetime, int]] = []
        sport_bins: list[tuple[datetime, int, int, float]] = []
        prev_day: str | None = None

        async with self._connection("poll", POLL_LOCK_WAIT_S) as conn:
            await conn.async_init()

            battery_reply = await conn.get_reply(yc.GET_POWER_STATISTICS)
            if battery_reply is not None:
                battery_level = yc_rec.parse_battery_percent(battery_reply.payload)

            steps_reply = await conn.get_reply(yc.GET_NOW_STEP)
            if steps_reply is not None:
                steps_total = yc_rec.parse_step_count(steps_reply.payload)

            # History: the HR stream is the canonical bpm series; the
            # all-metrics stream also carries hr and fills gaps on firmware
            # that logs through it. Sport drives the day totals.
            hr_fresh = await self._async_drain(
                conn, yc.HEALTH_HISTORY_HEART, yc_rec.parse_hr_records, "hr"
            )
            all_fresh = await self._async_drain(
                conn, yc.HEALTH_HISTORY_ALL, yc_rec.parse_all_records, "all"
            )
            sport_fresh = await self._async_drain(
                conn, yc.HEALTH_HISTORY_SPORT, yc_rec.parse_sport_records, "sport"
            )
            await self._async_save_hwm()

            # Skin temperature rides in the all-metrics records (layout
            # pinned in records.AllRecord); keep the newest reading so the
            # sensor survives polls whose drains hold none.
            temp_rows = [record for record in all_fresh if record.temperature]
            if temp_rows:
                latest_temp = max(temp_rows, key=lambda record: record.timestamp_ms)
                self._skin_reading = {
                    "skin_temperature": latest_temp.temperature,
                    "skin_temperature_timestamp": _ms_to_dt(latest_temp.timestamp_ms),
                }

            # HRV (ms, per the Yucheng SDK) rides in the same records, at
            # AllRecord.hrv; same keep-the-newest treatment as temperature.
            hrv_rows = [record for record in all_fresh if record.hrv]
            if hrv_rows:
                latest_hrv = max(hrv_rows, key=lambda record: record.timestamp_ms)
                self._hrv_reading = {
                    "hrv": latest_hrv.hrv,
                    "hrv_timestamp": _ms_to_dt(latest_hrv.timestamp_ms),
                }

            by_ts: dict[datetime, int] = {}
            for record in all_fresh:
                if record.hr:
                    by_ts[_ms_to_dt(record.timestamp_ms)] = record.hr
            for record in hr_fresh:
                by_ts[_ms_to_dt(record.timestamp_ms)] = record.bpm
            hr_entries = sorted(by_ts.items(), key=lambda row: row[0])

            today_entries = [
                (ts, bpm) for ts, bpm in hr_entries if date_utils.is_today(ts)
            ]
            if today_entries:
                heart_rate_timestamp, heart_rate = max(
                    today_entries, key=lambda x: x[0]
                )

            # Sport bins are (activity start, steps, meters, kcal); the
            # record start is the only timestamp they carry. Day totals sum
            # today's bins; the backfill manager takes all fresh bins and
            # dedupes by its own entity watermark.
            sport_bins = sorted(
                (
                    (
                        _ms_to_dt(record.start_time_ms),
                        record.steps,
                        record.distance_meters,
                        float(record.calorie_kcal),
                    )
                    for record in sport_fresh
                ),
                key=lambda row: row[0],
            )
            todays = [
                bin_row for bin_row in sport_bins if date_utils.is_today(bin_row[0])
            ]
            if todays:
                distance_total = sum(row[2] for row in todays)
                calories_total = round(sum(row[3] for row in todays))

            # The streams answer *whatever day* the ring holds, so a drained
            # record at-or-before yesterday means the backfill manager's
            # requested day was genuinely reached this poll.
            if include_previous_day:
                yesterday = date_utils.start_of_day(date_utils.now()) - timedelta(
                    days=1
                )
                oldest = [ts for ts, _ in hr_entries] + [
                    bin_row[0] for bin_row in sport_bins
                ]
                if oldest and min(oldest) <= yesterday:
                    prev_day = yesterday.date().isoformat()

        return {
            "heart_rate": heart_rate,
            "heart_rate_timestamp": heart_rate_timestamp,
            # No slot log to derive off-finger evidence from on this family.
            "hr_zero_reading": False,
            # No ring-side HR logging settings on this family.
            "hr_settings": None,
            "battery_level": battery_level,
            "battery_charging": battery_charging,
            "skin_temperature": (self._skin_reading or {}).get("skin_temperature"),
            "skin_temperature_timestamp": (self._skin_reading or {}).get(
                "skin_temperature_timestamp"
            ),
            "hrv": (self._hrv_reading or {}).get("hrv"),
            "hrv_timestamp": (self._hrv_reading or {}).get("hrv_timestamp"),
            "steps": steps_total,
            "distance": distance_total,
            "calories": calories_total,
            "hr_entries": hr_entries,
            "sport_bins": sport_bins,
            "prev_day": prev_day,
        }

    async def async_get_battery(self) -> dict[str, Any]:
        """Return battery level (charging state is not reported by the ring)."""
        async with self._connection("battery", USER_LOCK_WAIT_S) as conn:
            await conn.async_init()
            reply = await conn.get_reply(yc.GET_POWER_STATISTICS)
            return {
                "battery_level": (
                    yc_rec.parse_battery_percent(reply.payload)
                    if reply is not None
                    else None
                ),
                "charging": None,
            }

    async def async_set_time(self, value: datetime) -> None:
        """
        Sync the ring clock.

        The firmware stores wall-clock-as-UTC and stamps its records in the
        same local wall clock, so the ring must be set to *local* time (the
        codec's timestamp fix then converts records back to true UTC).
        """
        local = value.astimezone() if value.tzinfo else value
        async with self._connection("set-time", USER_LOCK_WAIT_S) as conn:
            await conn.send(yc_pkt.setting_time(local))

    async def async_set_monitor_interval(self, minutes: int) -> int:
        """
        Set the ring's background monitoring interval.

        Applies the interval to all four all-day monitors (heart rate,
        blood pressure, temperature, blood oxygen) so they share one
        schedule. Unlike the Colmi settings command there is no read
        opcode, so the accepted value is confirmed by the heart-rate
        monitor's opcode echo (this family answers every Setting command
        with a generic 1-byte status echoing its opcode); a 0xFC refusal
        of that opcode leaves no echo either, so it fails the same check.
        A refusal on any *other* monitor opcode is that variant skipping a
        feature and does not fail the write. Returns the clamped value
        actually written; raises when the ring refused or never answered.
        """
        minutes = clamp_ycbt_monitor_interval(minutes)
        async with self._connection("interval", USER_LOCK_WAIT_S) as conn:
            conn.clear_replies()
            for packet in (
                yc_pkt.set_monitor_interval(minutes),
                yc_pkt.set_blood_pressure_monitor(minutes),
                yc_pkt.set_temperature_monitor(minutes),
                yc_pkt.set_blood_oxygen_monitor(minutes),
            ):
                await conn.send(packet)
                await asyncio.sleep(0.15)
            reply = await conn.get_reply(yc.SET_MONITOR_INTERVAL)
            if reply is None:
                msg = (
                    f"Yucheng ring {self._ring_address} did not accept "
                    f"monitoring interval {minutes} min"
                )
                raise ColmiRingApiClientCommunicationError(msg)
        # Keep our copy so later connects re-apply it even before the entry
        # reload triggered by the options write lands.
        self._monitor_interval = minutes
        return minutes

    async def async_take_spo2(self) -> dict[str, Any]:
        """
        Take an on-demand SpO2 measurement and return the median sample.

        Returns a dict with ``spo2`` (int %) and ``spo2_timestamp`` (aware
        datetime), or ``spo2`` = None if the ring produced no valid reading.
        """
        samples = await self._async_measure(
            "spo2", "blood_oxygen", yc.REAL_UPLOAD_BLOOD_OXY
        )
        if not samples:
            return {"spo2": None, "spo2_timestamp": None}
        return {"spo2": _median(samples), "spo2_timestamp": date_utils.now()}

    async def async_take_heart_rate(self) -> dict[str, Any]:
        """
        Take an on-demand real-time heart-rate measurement.

        Live values arrive on the vendor ``0x0601`` push, the standard
        ``0x2A37`` HRS characteristic, or both, depending on firmware; both
        are collected and pooled into the median.
        """
        samples = await self._async_measure(
            "live-hr", "heart_rate", yc.REAL_UPLOAD_HEART
        )
        if not samples:
            return {"heart_rate": None, "heart_rate_timestamp": None}
        return {
            "heart_rate": _median(samples),
            "heart_rate_timestamp": date_utils.now(),
        }

    async def async_take_temperature(self) -> dict[str, Any]:
        """
        Take an on-demand skin-temperature measurement.

        The R99's live temperature push opcode is undocumented, so the
        session collects any frame byte in the plausible whole-degC range
        (see :meth:`_async_measure`); the median resists the occasional
        unrelated byte that happens to fall in range. Returns a dict with
        ``skin_temperature`` (degC) and ``skin_temperature_timestamp``,
        or ``skin_temperature`` = None if nothing plausible appeared.
        """
        samples = await self._async_measure(
            "temperature", "temperature", yc.REAL_UPLOAD_BODY_DATA
        )
        if not samples:
            return {"skin_temperature": None, "skin_temperature_timestamp": None}
        reading = {
            "skin_temperature": float(_median(samples)),
            "skin_temperature_timestamp": date_utils.now(),
        }
        # Persist so the next poll's (possibly empty) history drain does
        # not drop the reading the user just asked for. A failed session
        # leaves any earlier reading standing instead of clearing it.
        self._skin_reading = reading
        return reading

    async def _async_measure(
        self,
        purpose: str,
        type_name: str,
        push_opcode: int,
    ) -> list[int]:
        """
        Prime + start a manual measurement and collect its live pushes.

        Runs the same flow as the Phase-0 probe: enable sensors (init
        burst), ``0x030C`` prime, ``0x032F`` start, watch the metric's
        push opcode (and HRS when present), then always ``0x032F`` stop.

        SpO2 and temperature on the R99 need extra collection paths: the
        ring streams per-second ``0x060D`` progress frames whose value
        bytes stay zero until it computes a reading, can run past the
        ordinary window, answers the ``0x0211`` ("IS") real-time query
        (SpO2 only), and may carry the final value in the ``0x040E``
        completion payload. SpO2 keeps its verified ``0x0602`` push plus
        these extras; temperature has no verified push opcode, so every
        frame is scanned for bytes in the plausible range instead. Each
        path accepts only in-range bytes, and the median resists the
        occasional coincidental one.

        Returns the raw samples; empty means the ring never produced a
        plausible value.
        """
        samples: list[int] = []
        spo2 = type_name == "blood_oxygen"
        temp = type_name == "temperature"
        scan = spo2 or temp
        plausible = TEMP_PLAUSIBLE if temp else SPO2_PLAUSIBLE
        completed = False
        log_context = f"{self._ring_address} {type_name}"
        async with self._connection(purpose, USER_LOCK_WAIT_S) as conn:
            await conn.async_init()

            def on_frame(opcode: int, packet: yc_pkt.YcbtPacket) -> None:
                nonlocal completed
                if opcode == push_opcode and not temp:
                    if packet.payload:
                        value = packet.payload[0] & 0xFF
                        # 0 is "no reading yet"; 255 is the SDK's sentinel
                        # for sensor-off/garbage on both HR and SpO2 pushes.
                        if 0 < value < 255:
                            samples.append(value)
                    return
                if opcode in (
                    yc.MEASUREMENT_COMPLETE,
                    yc.MEASUREMENT_STATUS,
                ):
                    LOGGER.debug(
                        "Yucheng ring %s measurement %s event 0x%04X: payload=%s",
                        self._ring_address,
                        type_name,
                        opcode,
                        packet.payload.hex(),
                    )
                    completed = True
                    # The completion layout is undocumented and may carry
                    # timestamp bytes that look plausible, so trust it only
                    # when nothing else produced a sample.
                    if scan and not samples:
                        samples.extend(
                            _measure_extra_samples(
                                opcode,
                                packet,
                                log_context,
                                plausible,
                            ),
                        )
                elif scan:
                    samples.extend(
                        _measure_extra_samples(
                            opcode,
                            packet,
                            log_context,
                            plausible,
                            fallback=temp,
                        ),
                    )

            def on_hrs(bpm: int) -> None:
                if 0 < bpm < 255:
                    samples.append(bpm)

            conn.add_live_listener(on_frame)
            if type_name == "heart_rate" and conn.has_hrs:
                conn.add_hrs_listener(on_hrs)
                await conn.start_hrs()

            window = LONG_MEASURE_SECONDS if scan else MEASURE_SECONDS
            try:
                LOGGER.debug(
                    "Yucheng ring %s starting %s measurement"
                    " (prime 0x%04X, start 0x%04X type, watching push"
                    " 0x%04X, window %.0fs)",
                    self._ring_address,
                    type_name,
                    yc.APP_PRIME_SENSORS,
                    yc.APP_START_MEASUREMENT,
                    push_opcode,
                    window,
                )
                await conn.send(yc_pkt.prime_sensors())
                await asyncio.sleep(0.3)
                await conn.send(yc_pkt.start_measurement(type_name, start=True))

                async def poll_realtime() -> None:
                    await conn.send(yc_pkt.get_real_blood_oxygen())

                await _async_measure_window(
                    samples,
                    window,
                    lambda: completed,
                    poll=poll_realtime if spo2 else None,
                )
            finally:
                try:
                    await conn.send(yc_pkt.start_measurement(type_name, start=False))
                except ColmiRingApiClientError:
                    LOGGER.debug(
                        "Yucheng ring %s did not acknowledge measurement stop",
                        self._ring_address,
                    )
                conn.remove_live_listener(on_frame)
                conn.remove_hrs_listener(on_hrs)
                await conn.stop_hrs()
        LOGGER.debug(
            "Yucheng ring %s %s measurement collected %d sample(s): %s",
            self._ring_address,
            type_name,
            len(samples),
            samples[:20],
        )
        return samples

    async def async_probe_family(self) -> None:
        """
        Config-flow verification: connect once and prove it answers us.

        The ring is this family when it answers *any* init command. A few
        per-command 0xFC refusals on features a variant lacks are normal
        (battery/steps/name still land) and only get logged. When nothing
        is answered the two outcomes get distinct UI errors: every command
        refused with 0xFC - the firmware's "not bound" answer, or a ring
        that isn't really Yucheng - raises
        :class:`ColmiRingApiClientAuthenticationError`, while total silence
        raises :class:`ColmiRingApiClientCommunicationError` (a Colmi ring
        ignores unknown frames outright, so silence is the wrong-family or
        unreachable-ring case).
        """
        async with self._connection("probe", USER_LOCK_WAIT_S) as conn:
            answered, refused = await conn.async_init()
            if answered:
                return
            refused_text = ", ".join(f"0x{opcode:04X}" for opcode in sorted(refused))
            if refused:
                msg = (
                    f"Ring {self._ring_address} answered every init command"
                    f" with 0xFC 'not bound' (refused opcodes: {refused_text})"
                    " even after the bind retry; it is locked to another"
                    " central or is not a Yucheng ring"
                )
                raise ColmiRingApiClientAuthenticationError(msg)
            msg = (
                f"Ring {self._ring_address} connected but ignored every"
                " Yucheng command; it may not be a Yucheng (YCBT) ring"
            )
            raise ColmiRingApiClientCommunicationError(msg)
