"""
Colmi Ring API client.

This integration uses only the *pure* packet codecs (vendored into the local
``colmi_codec`` subpackage, originally from the ``colmi_r02_client`` project)
and owns its own Bluetooth transport. The transport is wired through Home
Assistant's Bluetooth stack (``homeassistant.components.bluetooth`` +
``bleak_retry_connector``) so
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
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from .colmi_codec import (
    battery,
    blink_twice,
    date_utils,
    hr,
    hr_settings,
    motion,
    r08,
    real_time,
    reboot,
    steps,
    stress,
)
from .colmi_codec import (
    set_time as set_time_mod,
)
from .colmi_codec.hr import HeartRateLogParser
from .colmi_codec.steps import SportDetailParser
from .colmi_codec.stress import StressLogParser
from .const import (
    LOGGER,
    MEASURE_HEART_RATE,
    MEASURE_HRV,
    MEASURE_SPO2,
    MOTION_STREAM_SILENCE_TIMEOUT_S,
    measurement_busy_message,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.descriptor import BleakGATTDescriptor
    from bleak.backends.device import BLEDevice
    from homeassistant.core import HomeAssistant


UART_SERVICE_UUID = "6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

# The Bluetooth HID Service, SIG 16-bit UUIDs in the same 128-bit form bleak
# reports. The R08's touch input is a HID *Consumer Control* report, so reaching
# it means talking to the HID service as well as the Colmi UART service above.
#
# These are not decoration: ``2a4c`` (HID Control Point) and ``2a4e`` (Boot
# Protocol Report Reference) are what put the ring into Report protocol, and the
# ring refuses the touch report's CCCD write until that has happened.
HID_CONTROL_POINT_CHAR_UUID = "00002A4C-0000-1000-8000-00805F9B34FB"
HID_BOOT_PROTOCOL_CHAR_UUID = "00002A4E-0000-1000-8000-00805F9B34FB"
HID_REPORT_CHAR_UUID = "00002A4D-0000-1000-8000-00805F9B34FB"
# Report Reference descriptor: two bytes, ``<report ID> <report type>``. Four of
# this ring's characteristics share the ``2a4d`` UUID, so this descriptor - not
# the UUID - is what names the touch report.
HID_REPORT_REFERENCE_DESC_UUID = "00002908-0000-1000-8000-00805F9B34FB"


def _is_sig_uuid(uuid: str, short: str) -> bool:
    """
    True when ``uuid`` is the SIG 16-bit UUID ``short``, in any spelling.

    Stacks differ in whether they hand back ``2a4d`` or the expanded 128-bit
    form (and in case), so matching one literal string is how a characteristic
    silently stops being found.
    """
    value = uuid.upper().replace("-", "")
    if value == short.upper():
        return True
    return len(value) == 32 and value.startswith(f"0000{short.upper()}")


def _write_needs_response(char: BleakGATTCharacteristic) -> bool:
    """
    Whether a write to ``char`` must ask for an acknowledgement.

    Bleak does not reliably infer this for these characteristics, and guessing
    wrong fails the write outright: the ring's UART RX is write-without-response
    as is the HID Control Point, while the Boot Protocol Report Reference is
    read/write and insists on a response. So the answer comes from what the
    device advertises.
    """
    props = set(char.properties)
    if "write-without-response" in props:
        return False
    return "write" in props


# How long to wait for a (possibly multi-packet) reply to a single command.
# The upstream library uses a fixed 2s which is too tight over a Bluetooth
# proxy, so we raise it.
REPLY_TIMEOUT = 10.0

# How long a real-time measurement waits for its FIRST usable sample. Silence
# alone is bounded by REALTIME_EMPTY_WAITS_MAX timeouts, but a zero-valued
# reply (the ring's "not measured yet") resets that counter, and a firmware
# without the reading keeps answering zero forever - measured on an R02 asked
# for real-time HRV. Zeros are also normal for a few seconds when a supported
# measurement warms up, so the wait for the first sample is bounded by time
# rather than rounds; once samples flow, only the timeout cap remains. Without
# some bound the loop never reaches STOP and leaves the sensor light blinking
# and the shared link held indefinitely.
REALTIME_FIRST_SAMPLE_S = 60.0
# Consecutive reply timeouts tolerated while streaming (upstream tolerates
# many; we cap them so a button press finishes in bounded time even when the
# ring never streams at all - per-sample timeout stays generous for proxy
# latency).
REALTIME_EMPTY_WAITS_MAX = 5
# The capability bitmap (the set-time reply) is a nicety: rings that answer
# command 1 do so at once, so a ring that does not answer is not worth
# delaying a poll for longer than this.
CAPABILITY_REPLY_TIMEOUT_S = 2.0
# How long a touch session waits for the ring to echo the ``3B 02`` enable frame
# it was just sent. That echo is the only acknowledgement the command offers:
# the reports it unlocks are notifications on a different characteristic and may
# not arrive for hours, so without the echo there is nothing to distinguish
# "this firmware accepted the enable and no gesture has been made" from "this
# firmware ignored the enable and nothing will ever arrive".
TOUCH_ECHO_TIMEOUT_S = 5.0

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
    range_minutes: int,
) -> bool:
    """
    True if the ring's newest *sample* read 0 bpm (off-finger evidence).

    ``range_minutes`` is the log's slot grid: R02-class firmware logs on a
    fixed 5-minute grid (samples sparser, zero slots between them), while
    the R08 grids the day by its sampling interval (one slot per sample,
    grid == interval). Slots between samples are also 0 ("no sample taken
    yet"), so a zero in the newest slot only counts when the newest filled
    slot is clearly newer than the newest non-zero sample. A *worn* ring can
    legitimately show a gap of one logging interval plus one slot: the
    ring's log index advances slot-by-slot while samples land only once per
    interval, so right after the index steps past a sample the newest filled
    slot sits exactly interval + range minutes ahead. Only a gap *beyond*
    that means the ring actually recorded a zero (and then kept advancing),
    i.e. it saw no pulse. Requiring ``> interval + range`` instead of
    ``>= interval`` fixes the intermittent false "Away" seen at interval
    boundaries.
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
        minutes=interval_minutes + range_minutes
    )


class ColmiRingApiClientError(Exception):
    """Exception to indicate a general API error."""


class ColmiRingApiClientCommunicationError(ColmiRingApiClientError):
    """Exception to indicate a communication error."""


class ColmiRingApiClientAuthenticationError(ColmiRingApiClientError):
    """Exception to indicate an authentication error."""


class ColmiRingApiClientBusyError(ColmiRingApiClientCommunicationError):
    """
    Exception to indicate the ring's single link is owned by another session.

    Not a failure of the ring: it means we asked for a second simultaneous
    session (poll vs. motion stream), which its firmware cannot serve. Callers
    that have data already should keep it rather than report an error.
    """


class _RingConnection:
    """
    A single active GATT connection to the ring, managed by HA's BT stack.

    The ring serves exactly one central, so this integration keeps at most one
    connection open at a time and lets every caller share it; that sharing -
    refcounting, the transaction lock, exclusive leases and the idle disconnect
    - lives in :class:`SharedLink`. This class is pure transport: connect,
    dispatch, command. Command handlers are installed per *transaction* (see
    :meth:`begin_transaction`) so stateful multi-packet parsers never leak
    between the users of one long-lived link.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        on_seen: Callable[[], None] | None = None,
    ) -> None:
        """
        Store the resolved HA instance and the ring MAC address.

        ``on_seen`` (if given) is invoked on every inbound GATT notification,
        feeding the coordinator's last-seen tracker. Synchronous and cheap by
        contract: it runs inside bleak's notification callback.
        """
        self._hass = hass
        self._address = address
        self._on_seen = on_seen
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
        # Second tap, fired *before* the 16-byte length filter, so the dev-only
        # debug probe (debug_probe.py) also sees frames the normal dispatch
        # drops. Deliberately separate from ``observer``: a stream session may
        # own that one while a probe captures alongside it.
        self.raw_observer: Callable[[bytearray], None] | None = None
        # Third tap, also above the length filter, but owned by a session rather
        # than the probe. The R08's touch reports are 3-byte HID reports, so the
        # 16-byte UART dispatch below cannot carry them: it would discard every
        # gesture as "a packet of unexpected length". Fired for notifications of
        # *any* length, because the ring has been observed multiplexing a 16-byte
        # UART echo onto the touch report characteristic too - an observer must
        # therefore check the length itself (see ``r08.TOUCH_REPORT_LEN``).
        self.touch_observer: Callable[[bytearray], None] | None = None
        # Monotonic timestamp of the most recent motion packet (see
        # ``add_motion_timestamp``); used by the stream watchdog.
        self.last_motion: float | None = None

    # Rings advertise roughly once per second, so HA's scanner normally
    # re-lists the device within a couple of poll attempts.
    _RESOLVE_TIMEOUT_S = 10.0
    _RESOLVE_POLL_S = 0.5

    async def _async_resolve_device(self) -> BLEDevice:
        """
        Resolve the ring to HA's BLEDevice, waiting briefly for a miss.

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
        except (TimeoutError, BleakError, OSError) as err:
            await self.close()
            raise ColmiRingApiClientCommunicationError(
                f"Could not connect to Colmi ring {self._address}: {err}"
            ) from err

        return self

    def begin_transaction(self) -> None:
        """
        Install the command handlers for one transaction on this link.

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
            set_time_mod.CMD_SET_TIME: set_time_mod.parse_set_time_packet,
            steps.CMD_GET_STEP_SOMEDAY: SportDetailParser().parse,
            stress.CMD_READ_STRESS: StressLogParser().parse,
        }
        self._queues = {cmd: asyncio.Queue() for cmd in self._handlers}

    def end_transaction(self) -> None:
        """Tear down the handlers installed by :meth:`begin_transaction`."""
        self._handlers = {}
        self._queues = {}

    def _on_disconnect(self, _client: BleakClient) -> None:
        """
        Best-effort: wake any waiting readers so they don't hang.

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

    def add_listener(self, cmd: int, listener: Callable[[bytearray], None]) -> None:
        """Receive *every* notification whose command byte is ``cmd``."""
        self._listeners.setdefault(cmd, []).append(listener)

    def remove_listener(self, cmd: int, listener: Callable[[bytearray], None]) -> None:
        """Undo :meth:`add_listener`; unknown listeners are ignored."""
        listeners = self._listeners.get(cmd)
        if listeners and listener in listeners:
            listeners.remove(listener)

    def touch_motion(self) -> None:
        """Record that motion data just arrived (feeds the stream watchdog)."""
        self.last_motion = time.monotonic()

    # ------------------------------------------------------------------
    # Dev-only debug probe helpers (see debug_probe.py). These deliberately
    # poke past the normal dispatch: the point of a probe is to see what the
    # ring actually sends, not only what this transport knows how to parse.
    # ------------------------------------------------------------------

    def debug_gatt_table(self) -> list[str]:
        """
        One line per service and characteristic, for the probe log.

        Handles are included because they, not UUIDs, identify a declaration:
        a Bluetooth HID device publishes several characteristics under one UUID
        (the R08 has four ``2a4d`` Reports), so a capture that names only UUIDs
        cannot say which of them anything happened on.
        """
        if self._client is None:
            return ["(no client)"]
        lines: list[str] = []
        for service in self._client.services:
            lines.append(f"service {service.uuid}")
            lines.extend(
                f"  char {char.uuid}#{char.handle} [{', '.join(char.properties)}]"
                for char in service.characteristics
            )
        return lines

    async def debug_subscribe_extras(
        self,
        on_notify: Callable[[BleakGATTCharacteristic, bytearray], None],
    ) -> tuple[list[BleakGATTCharacteristic], list[BleakGATTCharacteristic]]:
        """
        Subscribe to every other notify/indicate characteristic we can find.

        A probe hunting for an undocumented report (an R08 touch event, say)
        must not assume it arrives on the UART TX characteristic. Each
        characteristic is subscribed individually so one refusal (a firmware
        that errors on a CCCD write) does not lose the rest.

        Returns ``(subscribed, refused)`` *as characteristic objects* rather
        than UUIDs. Addressing by object is what distinguishes this ring's four
        Report declarations (``2a4d``, same UUID, different handles): addressed
        by UUID, both a subscription and its unsubscribe mean "whichever
        declaration the stack happens to find first", so the probe can end up
        listening on a report that is not the one carrying events. Refusals are
        returned rather than swallowed for the same reason - in a live capture
        exactly one of the two notify-capable ``2a4d`` declarations refused the
        CCCD write, and being able to retry that specific one (see
        :meth:`debug_subscribe_specific`) is what separates "this device is
        silent" from "the only report characteristic we could enable is silent".
        """
        assert self._client is not None
        candidates = [
            char
            for service in self._client.services
            for char in service.characteristics
            # The UART TX characteristic is already dispatched through
            # _handle_tx; anything else that can notify is fair game.
            if char.uuid.upper() != UART_TX_CHAR_UUID
            and {"notify", "indicate"} & set(char.properties)
        ]
        return await self.debug_subscribe_specific(candidates, on_notify)

    async def debug_subscribe_specific(
        self,
        chars: list[BleakGATTCharacteristic],
        on_notify: Callable[[BleakGATTCharacteristic, bytearray], None],
    ) -> tuple[list[BleakGATTCharacteristic], list[BleakGATTCharacteristic]]:
        """
        Subscribe to the given characteristics, one at a time.

        Split out from :meth:`debug_subscribe_extras` so a probe can retry a
        known set after changing the device's state: a peripheral may reject a
        CCCD write while its input source is idle and accept the same write
        once configured, so an early refusal is not necessarily a final one.
        """
        assert self._client is not None
        subscribed: list[BleakGATTCharacteristic] = []
        refused: list[BleakGATTCharacteristic] = []
        for char in chars:
            try:
                await self._client.start_notify(char, on_notify)
            except (TimeoutError, BleakError, OSError):
                refused.append(char)
                continue
            subscribed.append(char)
        return subscribed, refused

    async def debug_unsubscribe_extras(
        self,
        chars: list[BleakGATTCharacteristic],
    ) -> None:
        """Best-effort undo of :meth:`debug_subscribe_extras`."""
        if self._client is None:
            return
        for char in chars:
            try:
                await self._client.stop_notify(char)
            except (TimeoutError, BleakError, OSError):
                LOGGER.debug(
                    "Probe could not unsubscribe %s (handle %d)",
                    char.uuid,
                    char.handle,
                )

    async def debug_send(self, frame: bytearray) -> None:
        """Send one raw caller-built frame (the probe supplies the checksum)."""
        await self._send(frame)

    async def debug_write(self, uuid: str, data: bytearray) -> int:
        """
        Write one arbitrary characteristic by UUID, for the dev probe.

        Two write styles exist in the wild and picking wrong is an instant
        failure: the ring's UART RX characteristic is write-without-response,
        while a Bluetooth HID Control Point (``2a4c``) is too, but other
        characteristics insist on a response. Bleak does not reliably infer it
        here, so the choice comes straight from the characteristic's own
        advertised properties.

        Returns the handle actually written, because a UUID can name more than
        one declaration - this ring publishes four Report characteristics
        (``2a4d``) - and then "which one did I just write" is the question the
        capture turns on.
        """
        if self._client is None:
            raise ColmiRingApiClientError("not connected")
        char = self._client.services.get_characteristic(uuid)
        if char is None:
            raise ColmiRingApiClientError(f"no characteristic {uuid}")
        props = set(char.properties)
        if "write-without-response" in props:
            response = False
        elif "write" in props:
            response = True
        else:
            raise ColmiRingApiClientError(f"{uuid} is not writable ({char.properties})")
        await self._client.write_gatt_char(char, data, response=response)
        return char.handle

    async def debug_read(self, specifier: BleakGATTCharacteristic | int | str) -> bytes:
        """
        Read one characteristic, for the dev probe.

        Some answers are descriptors of the protocol rather than traffic: the
        Bluetooth HID Report Map (``2a4b``) says exactly how this ring's input
        reports are laid out, which no amount of packet sniffing will tell you
        if the device stays quiet during the capture.

        Accepts a characteristic object or handle as well as a UUID, for the
        same reason the subscription helpers do: duplicated UUIDs make "read
        this by UUID" mean "read whichever declaration the stack found first".
        """
        assert self._client is not None
        return bytes(await self._client.read_gatt_char(specifier))

    def debug_readable_chars(self) -> list[BleakGATTCharacteristic]:
        """
        Every characteristic with a readable value, for the probe's dump.

        Characteristics, not UUIDs: addressing by object is what distinguishes
        the ring's four ``2a4d`` Report declarations from one another.
        """
        if self._client is None:
            return []
        return [
            char
            for service in self._client.services
            for char in service.characteristics
            if "read" in char.properties
        ]

    def debug_gatt_chars(self) -> list[BleakGATTCharacteristic]:
        """Every characteristic on the link, for the dev probe's inspection."""
        if self._client is None:
            return []
        return [
            char
            for service in self._client.services
            for char in service.characteristics
        ]

    def debug_char_descriptors(
        self,
        char: BleakGATTCharacteristic,
    ) -> list[BleakGATTDescriptor]:
        """
        A characteristic's descriptors, for the dev probe to name and read.

        Descriptors are where a duplicated UUID stops being ambiguous: a
        Bluetooth HID Report characteristic carries a Report Reference
        (``2908``) holding its report ID and type (1 Input / 2 Output /
        3 Feature), which is what says which of four ``2a4d`` declarations is
        the input report, and therefore which one to listen to.
        """
        return list(char.descriptors)

    async def debug_read_descriptor(self, descriptor: BleakGATTDescriptor) -> bytes:
        """Read one descriptor, for the dev probe."""
        assert self._client is not None
        return bytes(await self._client.read_gatt_descriptor(descriptor.handle))

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
        except (TimeoutError, BleakError, OSError):
            LOGGER.debug("Ignoring disconnect error for %s", self._address)

    def _notify_observers(self, packet: bytearray) -> None:
        """
        Run the whole-packet taps, before any filtering of this packet.

        Three of them, each fenced: a callback raising here would otherwise
        propagate out of the bleak notification callback and stop notifications
        for the whole link, so no tap may be allowed to kill the others.

        - ``raw_observer`` is the debug probe's tap: it sees *every*
          notification, including lengths this transport discards.
        - ``touch_observer`` is a session's tap, placed above the length gate
          because a 3-byte HID touch report must not be discarded as "a packet
          of unexpected length" before the session that asked for it sees it.
        - ``_on_seen`` is not about the packet at all: any inbound notification
          is proof the ring is alive and in range, whether or not this packet is
          usable. The coordinator's throttle absorbs a stream's per-second
          flood.
        """
        if self.raw_observer is not None:
            try:
                self.raw_observer(packet)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring raw packet observer raised")
        if self.touch_observer is not None:
            try:
                self.touch_observer(packet)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring touch report observer raised")
        if self._on_seen is not None:
            try:
                self._on_seen()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring on-seen callback raised")

    def _handle_tx(self, char: BleakGATTCharacteristic, packet: bytearray) -> None:
        """
        Bleak notification callback: dispatch a packet to whoever wants it.

        Registered on the UART TX characteristic *and* (for an armed touch
        session) on the HID report characteristic, so the length test below is
        what tells the two streams of traffic apart rather than the callback.
        """
        self._notify_observers(packet)
        if len(packet) != 16:
            # HID touch reports are 3 bytes by design (the Consumer Control
            # report is 24 one-bit fields), so a session that is listening for
            # them owns short frames and the complaint would be pure noise at
            # gesture rate. Anything else of a surprising length is still named.
            if self.touch_observer is None or len(packet) != r08.TOUCH_REPORT_LEN:
                LOGGER.debug(
                    "Ignoring packet of unexpected length (%d bytes) from %s: %s",
                    len(packet),
                    getattr(char, "handle", "?"),
                    packet.hex(" "),
                )
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
                    "Unexpected packet type %s (0x%02X) from ring during %s: %s",
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
        except (TimeoutError, BleakError, OSError) as err:
            raise ColmiRingApiClientCommunicationError(
                f"Lost connection to Colmi ring {self._address}"
                " (out of range or disconnected)"
            ) from err

    async def _command(self, packet: bytearray, cmd: int) -> Any:
        """
        Send a command and await its (final) parsed reply.

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
            done, _ = await asyncio.wait({reply, dropped}, timeout=REPLY_TIMEOUT)
            if not done:
                raise ColmiRingApiClientCommunicationError(
                    f"Timed out waiting for command {cmd} from ring {self._address}"
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
        return await self._command(stress.read_stress_packet(), stress.CMD_READ_STRESS)

    async def get_steps(
        self, target: datetime, today: datetime | None = None
    ) -> list[steps.SportDetail] | steps.NoData:
        """Read the step/sport log for ``target`` (day offset from ring clock)."""
        if today is None:
            today = datetime.now(tz=UTC)
        if target.tzinfo != UTC:
            target = target.astimezone(tz=UTC)
        days = (today.date() - target.date()).days
        return await self._command(
            steps.read_steps_packet(days), steps.CMD_GET_STEP_SOMEDAY
        )

    async def set_time(self, ts: datetime) -> dict[str, Any] | None:
        """
        Sync the ring's clock; return the capability reply, if it answers.

        The clock write itself is fire-and-forget, but the firmware answers
        command 1 with a capability bitmap (``parse_set_time_packet``), so we
        collect that reply here rather than leave it on the wire. A ring that
        never answers yields ``None`` after a short wait, which keeps the
        poll's timing for firmwares that stay silent.
        """
        await self._send(set_time_mod.set_time_packet(ts))
        try:
            return await asyncio.wait_for(
                self._queues[set_time_mod.CMD_SET_TIME].get(),
                timeout=CAPABILITY_REPLY_TIMEOUT_S,
            )
        except TimeoutError:
            return None

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
        """
        Ask the ring to stop streaming; errors are best-effort ignored.

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
        """
        Toggle the ring's on-device camera-button (wave) detection.

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

    # ------------------------------------------------------------------
    # R08 touch control (codec in colmi_codec/r08.py). Enabling it is an
    # ordinary Colmi UART command, but the reports it unlocks are Bluetooth HID
    # notifications on a characteristic that is *not* the UART TX one, so the
    # path needs three things the rest of this transport has never needed:
    # naming one of four same-UUID Report declarations, the HID handshake that
    # makes the ring accept a CCCD write at all, and a tap on the notification
    # callback above the 16-byte length filter (see ``touch_observer``).
    # ------------------------------------------------------------------

    async def _read_report_reference(
        self, char: BleakGATTCharacteristic
    ) -> bytes | None:
        """
        This Report characteristic's ``2908`` value, or None if it refused it.

        Refused is a real answer here and not a bug: on a measured R08 three of
        the four ``2a4d`` declarations answered the descriptor read and the one
        carrying touch answered *error 5, Insufficient authentication* - the
        same failure that makes a plain read of that characteristic fail, while
        notifications on it work perfectly well unencrypted.
        """
        assert self._client is not None
        descriptor = next(
            (desc for desc in char.descriptors if _is_sig_uuid(desc.uuid, "2908")),
            None,
        )
        if descriptor is None:
            return None
        try:
            return bytes(await self._client.read_gatt_descriptor(descriptor.handle))
        except (TimeoutError, BleakError, OSError):
            return None

    async def resolve_touch_report_char(self) -> BleakGATTCharacteristic:
        """
        Find the HID Report characteristic carrying touch, or raise.

        This ring publishes four characteristics under the ``2a4d`` Report UUID,
        and bleak's ``get_characteristic`` answers with whichever the stack
        enumerated first - so a listener addressed by UUID can subscribe to the
        (silent) Mouse Input declaration and conclude the ring reports nothing.
        The Report Reference descriptor is what names a declaration, holding
        ``<report ID> <report type>``; measured on an R08, touch is ID 4 type 1
        (Input) on handle 68.

        Because that read is auth-gated on the one declaration we want, an
        unreadable descriptor on a notify-capable ``2a4d`` keeps the candidate
        as a fallback rather than discarding it. Every candidate is logged with
        its descriptor outcome: this is the most fragile step in the feature, so
        a firmware revision that reorders handles has to be diagnosable from the
        log rather than inferred from silence.
        """
        if self._client is None:
            raise ColmiRingApiClientError("not connected")
        candidates = [
            char
            for service in self._client.services
            for char in service.characteristics
            if _is_sig_uuid(char.uuid, "2A4D") and "notify" in set(char.properties)
        ]
        fallback: BleakGATTCharacteristic | None = None
        for char in candidates:
            reference = await self._read_report_reference(char)
            LOGGER.debug(
                "Colmi ring %s touch-report candidate handle %d (%s): 2908=%s",
                self._address,
                char.handle,
                ", ".join(char.properties),
                reference.hex(" ") if reference else "(unreadable)",
            )
            if reference is None:
                # No descriptor at all and a refused read look the same from
                # here; only a declaration that has descriptors can be the
                # auth-gated touch one, so only those become the fallback.
                if char.descriptors and fallback is None:
                    fallback = char
                continue
            if (
                len(reference) >= 2
                and reference[0] == r08.TOUCH_REPORT_ID
                and reference[1] == r08.HID_REPORT_TYPE_INPUT
            ):
                return char
        if fallback is not None:
            LOGGER.debug(
                "Colmi ring %s touch report chosen by fallback: handle %d"
                " (Report Reference unreadable, no declaration read %02X %02X)",
                self._address,
                fallback.handle,
                r08.TOUCH_REPORT_ID,
                r08.HID_REPORT_TYPE_INPUT,
            )
            return fallback
        msg = (
            f"Ring {self._address} exposes no notify-capable"
            f" {HID_REPORT_CHAR_UUID} Report characteristic"
        )
        raise ColmiRingApiClientCommunicationError(msg)

    async def hid_touch_handshake(self) -> None:
        """
        Put the ring into Report protocol, so it will accept a CCCD write.

        Measured on an R08 as the sequence that starts touch reporting:
        ``2a4c = 01`` (Suspend) then ``2a4c = 00`` (Resume) to bounce the HID
        Control Point, then ``2a4e = 01`` to move the Boot Protocol Report
        Reference off Boot (its cold state) and onto Report protocol, which is
        what a HOGP host uses.

        It has to be redone on *every* connection, not just the first: the ring
        boots back to ``00``, and a reconnect that skips this is silent in a way
        that reads as "the feature is broken" instead of "the handshake was
        skipped". These writes go to HID characteristics by object, never to the
        UART ``_rx_char``, and the write style comes from the advertised
        properties because the two differ (``2a4c`` is write-without-response,
        ``2a4e`` is read/write).
        """
        if self._client is None:
            raise ColmiRingApiClientError("not connected")
        control_point = self._client.services.get_characteristic(
            HID_CONTROL_POINT_CHAR_UUID
        )
        boot_protocol = self._client.services.get_characteristic(
            HID_BOOT_PROTOCOL_CHAR_UUID
        )
        if control_point is None or boot_protocol is None:
            msg = (
                f"Ring {self._address} does not expose the Bluetooth HID control"
                " characteristics that touch reporting needs"
            )
            raise ColmiRingApiClientCommunicationError(msg)
        sequence = (
            (control_point, bytearray([0x01])),  # Suspend
            (control_point, bytearray([0x00])),  # Resume / normal mode
            (boot_protocol, bytearray([0x01])),  # Report protocol, not Boot
        )
        for char, payload in sequence:
            props = set(char.properties)
            if "write" not in props and "write-without-response" not in props:
                msg = (
                    f"Ring {self._address} HID characteristic {char.uuid}"
                    f"#{char.handle} is not writable ({char.properties})"
                )
                raise ColmiRingApiClientCommunicationError(msg)
            try:
                await self._client.write_gatt_char(
                    char, payload, response=_write_needs_response(char)
                )
            except (TimeoutError, BleakError, OSError) as err:
                raise ColmiRingApiClientCommunicationError(
                    f"Ring {self._address} refused the HID touch handshake"
                    f" ({char.uuid}#{char.handle} <- {payload.hex(' ')}): {err}"
                ) from err

    async def start_touch_notify(self, char: BleakGATTCharacteristic) -> None:
        """
        Subscribe to the touch report characteristic, reusing ``_handle_tx``.

        One notification callback for the whole link is deliberate. The ring has
        been observed multiplexing a 16-byte UART echo onto the report
        characteristic, so a callback that reasoned "this arrived on the HID
        characteristic, so it must be a report" would hand the session UART
        frames. A single dispatch point that re-tests the length is the only
        honest place for that check.

        Must run *after* :meth:`hid_touch_handshake`: measured on an R08 the ring
        rejects this CCCD write before the handshake and accepts it after, so a
        session that subscribes first hears nothing in exactly the way a session
        on firmware without touch does.
        """
        assert self._client is not None
        try:
            await self._client.start_notify(char, self._handle_tx)
        except (TimeoutError, BleakError, OSError) as err:
            raise ColmiRingApiClientCommunicationError(
                f"Ring {self._address} refused the touch report subscription"
                f" ({char.uuid}#{char.handle}): {err}"
            ) from err

    async def stop_touch_notify(self, char: BleakGATTCharacteristic) -> None:
        """Undo :meth:`start_touch_notify`; best-effort on a link already dying."""
        if self._client is None:
            return
        try:
            await self._client.stop_notify(char)
        except (TimeoutError, BleakError, OSError):
            LOGGER.debug(
                "Could not unsubscribe touch reports on ring %s (handle %d)",
                self._address,
                char.handle,
            )

    async def set_touch_control(
        self,
        enabled: bool,
        *,
        app_type: int = r08.TOUCH_APP_TYPE_MEDIA,
        sleep_minutes: int = r08.DEFAULT_TOUCH_SLEEP_MINUTES,
    ) -> str:
        """
        Turn the ring's touch input on or off, and say how that went.

        Returns ``"armed"``, ``"unwritten"``, ``"no-echo"`` or ``"disarmed"``
        rather than a bool, because the two ways enabling fails deserve to be
        told apart: ``"unwritten"`` means the frame never reached the ring, which
        is a link problem and worth another attempt, while ``"no-echo"`` means
        the ring took the frame and answered nothing, which is evidence that this
        firmware does not implement the command and will not be fixed by
        reconnecting.

        Enabling is confirmed by the ring echoing the frame it was sent - the
        only acknowledgement on offer, since the reports that follow are
        notifications on another characteristic and may not arrive for hours.
        The echo wait runs while the caller holds :meth:`SharedLink.gate`, which
        is what bounds its cost: on a ring that ignores the command, polls queue
        behind it for at most :data:`TOUCH_ECHO_TIMEOUT_S`.
        """
        packet = (
            r08.touch_config_packet(app_type, sleep_minutes)
            if enabled
            else r08.touch_off_packet()
        )
        LOGGER.debug(
            "Colmi ring %s touch-control packet (%s, appType %d, sleep %d min): %s",
            self._address,
            "on" if enabled else "off",
            app_type,
            sleep_minutes,
            packet.hex(" "),
        )
        if not enabled:
            # Teardown also runs after the link has dropped, where this write
            # would raise; a ring out of range has nothing to acknowledge.
            try:
                await self._send(packet)
            except ColmiRingApiClientError:
                LOGGER.debug(
                    "Could not tell ring %s to turn touch control off (already gone?)",
                    self._address,
                )
                return "unwritten"
            return "disarmed"

        echo = asyncio.Event()

        def on_reply(reply: bytearray) -> None:
            if r08.parse_touch_config_reply(reply):
                echo.set()

        self.add_listener(r08.CMD_TOUCH_CONTROL, on_reply)
        try:
            await self._send(packet)
            try:
                await asyncio.wait_for(echo.wait(), TOUCH_ECHO_TIMEOUT_S)
            except TimeoutError:
                return "no-echo"
        except ColmiRingApiClientError:
            return "unwritten"
        finally:
            self.remove_listener(r08.CMD_TOUCH_CONTROL, on_reply)
        return "armed"

    async def poll_real_time_reading(
        self, reading_type: real_time.RealTimeReading
    ) -> list[int]:
        """
        Start an on-demand real-time measurement, collect samples, stop it.

        The ring streams repeated readings (e.g. SPO2 %) after a START packet;
        we gather a handful of non-zero samples then send STOP. Returns the
        collected samples (possibly empty if none were valid). Mirrors the
        upstream driver's poll loop, except that the wait for samples is
        bounded by time as well as by timeouts, so the STOP below is always
        reached and the loop can never hang on a ring that streams only zeros.
        """
        queue = self._queues[real_time.CMD_START_REAL_TIME]
        # Drop any stale samples from a previous measurement.
        while not queue.empty():
            queue.get_nowait()

        await self._send(real_time.get_start_packet(reading_type))

        valid: list[int] = []
        empty_waits = 0
        # A zero reply means "measuring, no value yet": the ring is alive, so
        # zeros do not count toward the timeout cap - but they must not keep
        # the loop alive forever either. Upstream reset its timeout counter on
        # any packet, which let a firmware that answers a real-time HRV
        # request with permanent zeros (measured on an R02, whose firmware
        # lacks HRV) spin this loop forever: no STOP sent, the sensor light
        # blinking on the finger, the shared link held, and the button's
        # finally block never clearing the ring's busy flag. The stall
        # deadline re-arms on every usable sample, so a supported reading may
        # take its time warming up (or pause mid-stream) without being cut
        # off, while a perpetual-zero firmware gives up in ~a minute.
        stalled_deadline = time.monotonic() + REALTIME_FIRST_SAMPLE_S
        while len(valid) < 6 and empty_waits < REALTIME_EMPTY_WAITS_MAX:
            remaining = stalled_deadline - time.monotonic()
            if remaining <= 0:
                break
            # On a shared link the connection can drop mid-measurement; noticing
            # it here beats timing out a few more times on a queue nothing will
            # ever feed again.
            if self.dead.is_set():
                raise ColmiRingApiClientCommunicationError(
                    f"Link to Colmi ring {self._address} dropped during"
                    f" real-time {reading_type.name} measurement"
                )
            try:
                data: (
                    real_time.Reading | real_time.ReadingError
                ) = await asyncio.wait_for(
                    queue.get(), timeout=min(REPLY_TIMEOUT, remaining)
                )
            except TimeoutError:
                empty_waits += 1
                continue
            if isinstance(data, real_time.ReadingError):
                await self._send(real_time.get_stop_packet(reading_type))
                raise ColmiRingApiClientError(
                    f"Ring returned error code {data.code} for real-time "
                    f"reading {reading_type.name}"
                )
            if data.value == 0:
                # Alive but not yet measured: clears the timeout streak (the
                # ring answered) without extending the stall deadline.
                empty_waits = 0
                continue
            valid.append(data.value)
            empty_waits = 0
            stalled_deadline = time.monotonic() + REALTIME_FIRST_SAMPLE_S

        await self._send(real_time.get_stop_packet(reading_type))
        return valid


class SharedLink:
    """
    The one GATT link to a ring, shared by every caller in this client.

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

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        on_seen: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._address = address
        # Forwarded to every connection this link opens, so each inbound
        # notification can feed the coordinator's last-seen tracker.
        self._on_seen = on_seen
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
                conn = _RingConnection(self._hass, self._address, self._on_seen)
                await conn.connect()
                self._conn = conn
            conn.purpose = purpose
            return conn

    def _busy(self, purpose: str, holder: str) -> ColmiRingApiClientBusyError:
        return ColmiRingApiClientBusyError(
            f"Ring {self._address} is busy with {holder} (this one is a {purpose})"
        )

    @asynccontextmanager
    async def acquire(
        self, purpose: str, wait_s: float
    ) -> AsyncIterator[_RingConnection]:
        """
        One serialised command exchange on the shared link.

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
        """
        Hold the shared link open for a long-lived session.

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
            timed_out = False
            try:
                deadline = time.monotonic() + wait_s
                while self._tx_lock.locked():
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    await asyncio.sleep(self._LOCK_POLL_S)
                if not timed_out:
                    await self._tx_lock.acquire()
            except BaseException:
                self._exclusive = False
                self._maybe_idle()
                raise
            if timed_out:
                # Same unwind as the BaseException arm above: the original
                # raise sat inside the try, so release the claim before
                # failing the caller.
                self._exclusive = False
                self._maybe_idle()
                raise self._busy(purpose, "another transaction")
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
        """
        One locked write from inside a shared lease (wave arm/disarm).

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
    """
    Counts notifications seen by one stream session, by command byte.

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
            ", ".join(f"{name} x{num}" for name, num in self._by_type.most_common(5))
            or "none"
        )


class _TouchCounts:
    """
    What one touch session was told about, in terms a log line can use.

    :class:`_StreamTally` keys everything by UART command byte, which is the
    right question for a stream; for touch the questions are different - how
    many HID reports arrived, and how many were the *all-zero release* report
    that follows every press rather than a press itself. Runs inside the
    notification callback, so like the tally it only counts.
    """

    def __init__(self) -> None:
        self.reports = 0
        self.releases = 0
        self.events = 0
        self.gestures: Counter[str] = Counter()
        self.unknown_usages: Counter[int] = Counter()

    def add(self, packet: bytearray) -> None:
        """Record one notification of any length; report only what is a report."""
        report = r08.parse_touch_report(packet)
        if report is None:
            # Either not a report at all, or the release that follows every
            # press. Distinguishing the two needs the length, since a UART frame
            # is not a release, just something else arriving on the same link.
            if len(packet) == r08.TOUCH_REPORT_LEN:
                self.releases += 1
            return
        self.reports += 1
        self.events += 1
        for gesture in report.gestures:
            self.gestures[gesture] += 1
        # Bits the usage table does not name. The table covers 0.0..0.18, so a
        # set bit above that is a field the Report Map declares and we do not
        # model: the ring pressed something we cannot even name. Logged rather
        # than guessed at, because a misnamed gesture is worse than an unknown
        # one - and this counter is how that gap surfaces at all.
        unknown = report.raw >> len(r08.TOUCH_USAGE_BITS)
        bit = len(r08.TOUCH_USAGE_BITS)
        while unknown:
            if unknown & 1:
                self.unknown_usages[bit] += 1
            unknown >>= 1
            bit += 1

    def describe(self) -> str:
        """Human-readable summary of the gestures seen, most frequent first."""
        return (
            ", ".join(f"{name} x{num}" for name, num in self.gestures.most_common(5))
            or "none"
        )


class _LinkSession:
    """
    One open connection to the ring that its holder may end early.

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
    """
    A live RAW-motion session: one open connection with the stream on.

    Ends by itself if the stream goes silent, since a stream that reports
    nothing is not a working stream.
    """


class WaveSession(_LinkSession):
    """
    A live on-device wave session: one connection with camera mode armed.

    Silence is the normal state here - the ring reports one packet per wave and
    nothing in between - so this kind of session is never closed for lack of
    traffic, only for a dropped link.
    """


class TouchSession(_LinkSession):
    """
    A live touch session: one connection with the HID report subscribed.

    Silence is normal here too, and for longer: between gestures there is
    nothing, possibly for hours, so this kind of session is never closed for
    lack of traffic.

    It carries the report characteristic it subscribed to because that handle is
    resolved at session open (see
    :meth:`_RingConnection.resolve_touch_report_char`) and must be the same one
    unsubscribed on the way out: addressing by UUID would name whichever
    declaration the stack finds first, which is how a teardown leaves the wrong
    report live on the ring.
    """

    def __init__(
        self, conn: _RingConnection, report_char: BleakGATTCharacteristic
    ) -> None:
        super().__init__(conn)
        self.report_char = report_char


# Any kind of session, so the shared retry loop can be typed against all of them.
_SessionT = TypeVar("_SessionT", bound=_LinkSession)


@dataclass(frozen=True)
class _SessionKind[SessionT: _LinkSession]:
    """
    How one kind of long-lived session is opened, and when to reopen it.

    The motion stream, the wave listener and the touch listener hold the ring's
    link in the same way and want the same retry behaviour; what differs is the
    opening ritual and which closing reasons are worth a reconnect, so those
    three things travel together instead of as loose parameters.
    """

    #: What this kind of session is called in log lines.
    label: str
    #: Returns the context manager that holds the session open.
    opener: Callable[
        [Callable[[bytearray], None]],
        AbstractAsyncContextManager[SessionT],
    ]
    #: Closing reasons that mean "reconnect and try again"; anything else (a
    #: user turning the switch off, a ring that refuses the enable) ends the loop.
    retry_reasons: tuple[str, ...]


class ColmiRingApiClient:
    """High level client for a Colmi ring, using HA-managed BLE connections."""

    def __init__(
        self,
        hass: HomeAssistant,
        ring_address: str,
        on_seen: Callable[[], None] | None = None,
    ) -> None:
        """
        Store the HA instance and ring address (no connection yet).

        ``on_seen`` reaches every inbound notification (see
        :class:`_RingConnection`); the setup passes the coordinator's
        throttled last-seen tracker.
        """
        self._hass = hass
        self._ring_address = ring_address
        # Every session to this ring goes through here. The firmware serves one
        # central, so this client keeps one shared connection and serialises
        # command exchanges on it (see :class:`SharedLink`); long-lived
        # sessions hold it as a lease instead of owning it outright.
        self._link = SharedLink(hass, ring_address, on_seen=on_seen)
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
        # The ``3B 02`` enable the most recent touch session armed with, as
        # ``(app_type, sleep_minutes)``, or None if none ever went out. The
        # watchdog may need to re-send that same frame after the ring's own
        # touch sleep expires, and re-sending a *different* config than the one
        # the session armed with would quietly change the behaviour partway
        # through - so the session records what it wrote rather than each
        # caller re-deriving it from options.
        self._touch_config: tuple[int, int] | None = None

    async def async_close(self) -> None:
        """
        Tear the shared link down now (entry unload/reload).

        Left alone, the link would drop a few seconds after its last user
        leaves via the idle timer; unloading should not leave a live GATT
        connection owned by nobody in the meantime.
        """
        await self._link.close()

    @property
    def debug_link(self) -> SharedLink:
        """The shared link, exposed for the dev-only debug probe service."""
        return self._link

    def _claim_measurement(self, name: str) -> None:
        """
        Take the ring's one-at-a-time measurement slot or refuse loudly.

        The transaction lock would serialize two measurements anyway, but
        waiting on it means the second press hangs for the first one's whole
        (~30 s) duration and then reports a vague busy timeout. Claiming the
        slot up front fails it instantly with a message that says why.
        """
        if self._measuring is not None:
            raise ColmiRingApiClientBusyError(measurement_busy_message(self._measuring))
        self._measuring = name

    async def async_get_data(  # noqa: PLR0915 - one straight-line poll of every capability
        self, include_previous_day: bool = False
    ) -> dict[str, Any]:
        """
        Return the latest heart rate plus the ring's HR logging settings.

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
        # From the capability bitmap in the set-time reply: True/False once the
        # ring has answered command 1, None while unknown. None keeps the HRV
        # button available (no false negatives); a definite False disables it.
        supports_hrv: bool | None = None
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
            # The reply doubles as the ring's capability bitmap: it says
            # whether this firmware has HRV at all (measured: R02 no, R06 yes),
            # which gates the Read HRV button instead of letting it run a
            # measurement the ring can only answer with zeros.
            caps = await conn.set_time(date_utils.now())
            if isinstance(caps, dict) and isinstance(caps.get("mSupportHrv"), bool):
                supports_hrv = caps["mSupportHrv"]
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
                # Heart rate is logged on the ring's own slot grid (5-min
                # slots on R02-class firmware; the sampling interval itself
                # on the R08), so many slots are 0 and must be filtered out
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
                slot_range = log.range
                # Slots started so far today (slot k covers minutes
                # range*k..range*(k+1)-1). minutes_so_far() is "minutes + 1",
                # so subtract it back before dividing: otherwise the clamp can
                # admit a not-yet-started slot, inflating the zero-evidence
                # gap by a spurious slot.
                slots_now = (
                    date_utils.minutes_so_far(date_utils.now()) - 1
                ) // slot_range + 1
                filled = min(max(log.index, 0), len(all_slots), slots_now)
                hr_zero_reading = _latest_sample_is_zero(
                    all_slots, entries, filled, interval, slot_range
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
                    heart_rate, heart_rate_timestamp = max(entries, key=lambda x: x[1])

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
            hr_entries, sport_bins, prev_day = await self._async_extend_previous_day(
                conn, include_previous_day, hr_entries, sport_bins
            )

        return {
            "heart_rate": heart_rate,
            "heart_rate_timestamp": heart_rate_timestamp,
            "hr_zero_reading": hr_zero_reading,
            "hr_settings": settings,
            "supports_hrv": supports_hrv,
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
        """
        Return the newest (stress, timestamp), refetching once per slot.

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

    async def _async_read_stress(self, conn: _RingConnection) -> dict[str, Any] | None:
        """
        Read today's stress log and return its newest sample, or None.

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
        """
        Merge yesterday's UTC-day ring logs into the backfill series.

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
                    (ts, bpm) for bpm, ts in log.heart_rates_with_times() if bpm > 0
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
                    (d.timestamp, d.steps, d.distance, d.calories / 1000) for d in sport
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
        """
        Write the ring's HR logging settings, then read them back to verify.

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
        """
        Take an on-demand SpO2 measurement and return the median sample.

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
            "spo2_timestamp": datetime.now(tz=UTC),
        }

    async def async_take_heart_rate(self) -> dict[str, Any]:
        """
        Take an on-demand real-time heart-rate measurement.

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
            "heart_rate_timestamp": datetime.now(tz=UTC),
        }

    async def async_take_hrv(self) -> dict[str, Any]:
        """
        Take an on-demand real-time heart-rate-variability measurement.

        Returns a dict with ``hrv`` (int ms) and ``hrv_timestamp`` (aware
        datetime), or ``hrv`` = None if the ring produced no valid reading.
        """
        self._claim_measurement(MEASURE_HRV)
        try:
            async with self._link.acquire("hrv", USER_LOCK_WAIT_S) as conn:
                samples = await conn.poll_real_time_reading(
                    real_time.RealTimeReading.HRV
                )
        finally:
            self._measuring = None
        if not samples:
            return {"hrv": None, "hrv_timestamp": None}
        samples.sort()
        median = samples[len(samples) // 2]
        return {
            "hrv": median,
            "hrv_timestamp": datetime.now(tz=UTC),
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
        """
        Hold the link with RAW streaming on, until stopped.

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
                with suppress(asyncio.CancelledError):
                    await watchdog
                conn.remove_listener(motion.CMD_RAW_MODE, listener)
                conn.observer = None
                self._log_stream_session(session, tally)
                await conn.stop_motion_stream()

    @asynccontextmanager
    async def _wave_session(
        self, on_packet: Callable[[bytearray], None]
    ) -> AsyncIterator[WaveSession]:
        """
        Hold the link with the ring's own wave detector armed.

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
                with suppress(asyncio.CancelledError):
                    await watchdog
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

    @asynccontextmanager
    async def _touch_session(
        self, on_packet: Callable[[bytearray], None]
    ) -> AsyncIterator[TouchSession]:
        """
        Hold the link with the ring's touch input on and its reports subscribed.

        This is the session behind the (R08-only) Touch Control switch. It is a
        shared lease like the wave listener - touch reports are notifications, so
        nothing about *listening* needs the link to itself - but opening it is a
        three-step ritual that has to happen in this order, measured on an R08:

        1. the UART ``3B 02`` enable, confirmed by the ring echoing the frame;
        2. the HID handshake (``2a4c`` bounce, ``2a4e = 01``), because the ring
           boots in Boot protocol and refuses a CCCD write until it is moved to
           Report protocol;
        3. the ``start_notify`` on the resolved Report characteristic.

        Subscribing first is refused, and a refused subscription is silent, so a
        session that inverted steps 2 and 3 would look exactly like firmware
        without touch. Step 2 is redone on *every* connection for the same
        reason: the ring forgets it on reconnect.

        The observer goes in before step 1 so the enable echo and any report that
        arrives immediately cannot fall on the floor, and it is cleared on every
        exit path: the shared link outlives this session, and leaving the tap in
        place would count reports for a logged-and-done tally. The matching
        ``3B 01`` goes out on every exit path too, because the ring keeps
        listening for touches (and keeps powering the capacitive sensor) until
        told otherwise.

        No silence timeout, like the wave listener: silence here means the ring
        is in nobody's hand, which is the normal case for hours at a time.
        """
        app_type, sleep_minutes = self._touch_config or (
            r08.TOUCH_APP_TYPE_MEDIA,
            r08.DEFAULT_TOUCH_SLEEP_MINUTES,
        )
        async with self._link.hold(
            "touch control", exclusive=False, wait_s=STREAM_LOCK_WAIT_S
        ) as conn:
            # Not inside a gate: descriptor reads are a handful of small GATT
            # reads on attributes the UART exchange never touches, and one of
            # them is expected to fail on purpose. Holding the transaction lock
            # across them would queue polls behind a session-opening scan.
            report_char = await conn.resolve_touch_report_char()
            session = TouchSession(conn, report_char)
            counts = _TouchCounts()
            listener = self._make_packet_listener(session, on_packet)

            def on_report(packet: bytearray) -> None:
                counts.add(packet)
                # Only reports are forwarded. The observer sits above the link's
                # length filter, so it also sees the UART frames this ring has
                # been observed multiplexing onto the report characteristic; the
                # touch caller has no use for those, and neither has the
                # gesture-detecting manager above it.
                if len(packet) == r08.TOUCH_REPORT_LEN:
                    listener(packet)

            # Everything that can raise from here on runs inside the try below.
            # The observer is already installed by now, so a session that dies
            # part-way through opening - a refused handshake, a busy gate -
            # would otherwise leave its tap attached to a link this session does
            # not own, counting reports for a tally that was never logged.
            conn.touch_observer = on_report
            # Set before anything can raise, so the teardown can always log a
            # real reason rather than tripping over an unbound name.
            armed = "unwritten"
            subscribed = False
            watchdog: asyncio.Task[None] | None = None
            try:
                async with self._link.gate("touch arm"):
                    armed = await conn.set_touch_control(
                        True, app_type=app_type, sleep_minutes=sleep_minutes
                    )
                if armed != "armed":
                    # No point subscribing to a report the ring was never told to
                    # send: that would hold the link open and report nothing. The
                    # reason is the session's closing reason, so the retry loop
                    # can tell a link that dropped the write from firmware that
                    # ignored it - only the former is worth another connection.
                    session.request_stop(armed)
                else:
                    async with self._link.gate("touch handshake"):
                        await conn.hid_touch_handshake()
                        await conn.start_touch_notify(report_char)
                    subscribed = True
                watchdog = asyncio.create_task(self._watch_link(session))
                yield session
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                    with suppress(asyncio.CancelledError):
                        await watchdog
                conn.touch_observer = None
                # Only what was actually subscribed gets unsubscribed, and by the
                # characteristic object rather than the UUID: four declarations
                # share the UUID, so unsubscribing whichever one the stack finds
                # first leaves the real one live on the ring.
                if subscribed:
                    await conn.stop_touch_notify(report_char)
                self._log_touch_session(session, counts, armed)
                try:
                    async with self._link.gate("touch disarm"):
                        await conn.set_touch_control(False)
                except ColmiRingApiClientError:
                    # Best-effort: the link may already be gone (which is often
                    # why this session ended), and then there is nothing to arm.
                    LOGGER.debug(
                        "Could not tell ring %s to turn touch control off",
                        self._ring_address,
                    )

    def _log_touch_session(
        self, session: TouchSession, counts: _TouchCounts, armed: str
    ) -> None:
        """Report what a finished touch session saw, and whether it could work."""
        lived = f"{time.monotonic() - session.started_at:.1f}s"
        reason = session.reason or "ABORTED (never stopped by anyone)"
        if armed != "armed":
            # INFO: this is the line that answers "I turned it on and nothing
            # happens", and it names which kind of failure it was - "unwritten"
            # is a link problem, "no-echo" means this firmware has no such
            # command and no amount of retrying will teach it one.
            LOGGER.info(
                "Colmi ring %s touch control closed after %s (%s): the ring"
                " never acknowledged the enable command (%s), so no gesture could"
                " be reported",
                self._ring_address,
                lived,
                reason,
                armed,
            )
            return
        unnamed = ", ".join(
            f"bit 0.{bit} x{num}" for bit, num in counts.unknown_usages.most_common(5)
        )
        if counts.reports:
            LOGGER.debug(
                "Colmi ring %s touch control closed (%s, after %s): %d"
                " report(s) + %d release(s), %d gesture(s): %s%s",
                self._ring_address,
                reason,
                lived,
                counts.reports,
                counts.releases,
                counts.events,
                counts.describe(),
                f"; unnamed usages: {unnamed}" if unnamed else "",
            )
            return
        # INFO, for the same reason as the wave session's equivalent: a session
        # that was armed, subscribed and alive yet heard not even a *release*
        # report is the evidence that this firmware does not report touches.
        LOGGER.info(
            "Colmi ring %s touch control closed after %s (%s) with %d report(s)"
            " and none of them a touch: %s%s. If you touched the ring during this"
            " session, this firmware probably does not report the gesture",
            self._ring_address,
            lived,
            reason,
            counts.releases,
            counts.describe(),
            f"; unnamed usages: {unnamed}" if unnamed else "",
        )

    @staticmethod
    async def _watch_link(session: _LinkSession) -> None:
        """
        Close the session when the link drops.

        The wave listener's only failure mode. The RAW stream's silence rule is
        deliberately absent: here, silence means "no waves", not "broken".
        """
        await session.conn.dead.wait()
        session.request_stop("disconnected")

    def _log_stream_session(self, session: MotionSession, tally: _StreamTally) -> None:
        """
        Report what a finished stream session actually saw.

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
        """
        Close the session if the link drops or the stream stops reporting.

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
        """
        Run motion-stream sessions, restarting them while still wanted.

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
        """
        Run wave-listener sessions, restarting them while still wanted.

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

    async def async_listen_touches(
        self,
        on_packet: Callable[[bytearray], None],
        should_run: Callable[[], bool],
        on_session: Callable[[TouchSession], None] | None = None,
        *,
        app_type: int = r08.TOUCH_APP_TYPE_MEDIA,
        sleep_minutes: int = r08.DEFAULT_TOUCH_SLEEP_MINUTES,
    ) -> str | None:
        """
        Run touch-listener sessions, restarting them while still wanted.

        The wave listener's loop over the touch session. Two things are
        different enough to be worth saying:

        The options travel through :attr:`_touch_config` rather than as opener
        arguments, because ``_SessionKind.opener`` is a one-argument contract
        shared with the other two kinds of session. Stashing them here keeps
        that contract intact and also means a session reopened by the retry loop
        re-arms with the frame the user last asked for, not a default.

        ``"no-echo"`` is deliberately *not* a retry reason. A ring that takes the
        enable frame and answers nothing is telling us it has no such command;
        reconnecting cannot add a command to its firmware, and burning 180 s of
        retries on it only delays polling resuming.

        Callers must gate this on :func:`supports_touch`: on a ring without the
        HID report characteristics this fails at session open, and would spend
        its whole retry budget finding out what the model name already said.
        """
        self._touch_config = (app_type, sleep_minutes)
        return await self._run_sessions(
            _SessionKind(
                label="touch control",
                opener=self._touch_session,
                # Silence is normal here, so it never closes a session; a
                # dropped link is retried, and so is an enable write that never
                # reached the ring, which may just have been a busy moment.
                retry_reasons=("disconnected", "unwritten"),
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
        """
        Hold one kind of session open, reopening it while it is wanted.

        Shared by the motion stream, the wave listener and the touch listener:
        they differ only in how the session is opened and in which closing
        reasons are worth another attempt, but all three hold the ring's single
        link for their whole life, so all three owe the host the same courtesy
        about retrying and about knowing when to stop.

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
                    failing_since, delay = self._next_retry(failing_since, delay)
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
                    failing_since, delay = self._next_retry(failing_since, delay)
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
                    failing_since, delay = self._next_retry(failing_since, delay)
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
        """
        Hand the open session to the caller, and say so if the arming fails.

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
        except Exception:
            LOGGER.exception(
                "Colmi ring session arming callback raised; closing the session"
            )
            raise

    @staticmethod
    def _session_lived(session: _LinkSession | None) -> float:
        """
        Seconds a finished session held its lease, or -1 if never opened.

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
        """
        Sleep out one retry pause; False means the retry budget is done.

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
