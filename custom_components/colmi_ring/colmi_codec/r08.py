"""
R08 touch-surface packets: the enable/disable frames and the HID report decode.

Two unrelated protocols meet in this module, which is worth stating up front
because the code has to keep them apart.

*The enable side is Colmi UART.* Command ``0x3B`` with sub-command ``0x02``
carries the touch configuration, in the same 16-byte checksummed frame as every
other Colmi command (see :mod:`.packet`). The frames are the ones the R08
Access Bridge app sends, transcribed from its ``AccessBridgeControlProtocol``:

===================================================  ==========================
``touchConfig(appType, sleepMinutes)``               ``3B 02 00 <appType> <min>``
``gestureOff()``                                     ``3B 02 01 00 00``
``touchWake(appType)``                               ``3B 02 02 <appType> 01``
===================================================  ==========================

The ring answers by echoing the frame back verbatim on the UART TX
characteristic, measured in the Phase 0 probe; so ``parse_touch_config_reply``
is a validity check on opcode and sub-type rather than a parser of anything new.

The same command can in principle also carry the ring's *own* news - the
surface sleeping or waking, the touch disabled - though no firmware has been
observed sending one unasked. ``parse_touch_notice`` below classifies such a
frame anyway, so a caller that hears one can say accurately what it meant.

*The report side is Bluetooth HID, not Colmi.* Once touch is configured and the
HID layer has been put into Report protocol, gestures arrive as **3-byte
notifications on the Consumer Control Input report** - on a different
characteristic entirely, with no Colmi framing, no command byte and no
checksum. The Report Map read during the probe declares 18 one-bit fields
(``75 01 95 18`` = 24 bits) of Consumer page usages, and there is no leading
report-ID byte, so the payload is exactly the bitmap.

Which means the length filter in :class:`..api._RingConnection` - correct for
every Colmi frame - discards these. Touch reports therefore never travel the
``add_listener`` dispatch; see ``api.py`` for the separate subscription path.

Like the rest of this subpackage this module is pure: no Home Assistant, no
Bluetooth, no I/O.

The swipe direction mapping is the one fact here that started life as a guess;
an on-device labelled test settled it. See :data:`USAGE_GESTURES`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .packet import make_packet

# Command and sub-command bytes for the touch control frame.
CMD_TOUCH_CONTROL = 0x3B
SUB_TOUCH = 0x02
SUB_TOUCH_CONFIG = 0x00
SUB_TOUCH_OFF = 0x01
SUB_TOUCH_WAKE = 0x02

# Byte 4 of a ``02 02`` frame: the two states the frame names, whether we send
# it (waking a configured surface) or the ring reports it by itself.
TOUCH_STATE_ASLEEP = 0x00
TOUCH_STATE_AWAKE = 0x01

# The documented media-key profile: what the R08 Access Bridge pins, and what
# the Phase 0 probe captured touch reports under. appType 0..7 are accepted by
# the firmware; only 1 has been observed to produce reports.
TOUCH_APP_TYPE_MEDIA = 1
# Access Bridge's "Touch fallback" debug profile. Reported to work, never
# captured here, and not needed: the media profile already emits the reports.
TOUCH_APP_TYPE_TOUCH_FALLBACK = 4

# The fallback touch sleep, in minutes, used only when a caller does not pass a
# value (the integration's ``CONF_TOUCH_TIMEOUT`` is now the single source that
# always supplies one). Kept consistent with that option's default of 60 rather
# than the OEM app's 0x05, so the wire-level fallback and the user-facing default
# agree. The range floor/ceiling are the firmware byte's own (1-255).
DEFAULT_TOUCH_SLEEP_MINUTES = 60

# How long a configured ring keeps reporting for, as a floor: the sleep byte is
# in minutes and 0 has never been observed to mean "never".
TOUCH_SLEEP_MINUTES_MIN = 1
TOUCH_SLEEP_MINUTES_MAX = 255


def touch_config_packet(
    app_type: int = TOUCH_APP_TYPE_MEDIA,
    sleep_minutes: int = DEFAULT_TOUCH_SLEEP_MINUTES,
) -> bytearray:
    """
    Frame that turns the touch surface on and names its report profile.

    ``app_type`` selects which HID usages the firmware lights up;
    ``sleep_minutes`` is how long the surface stays awake between touches.
    """
    return make_packet(
        CMD_TOUCH_CONTROL,
        bytearray([SUB_TOUCH, SUB_TOUCH_CONFIG, app_type & 0xFF, sleep_minutes & 0xFF]),
    )


def touch_off_packet() -> bytearray:
    """Frame that turns the touch surface off again.

    The ring remembers this setting across a disconnect, in both directions, so
    leaving it on after our session ends is a battery cost we introduced.
    """
    return make_packet(CMD_TOUCH_CONTROL, bytearray([SUB_TOUCH, SUB_TOUCH_OFF, 0, 0]))


def touch_wake_packet(app_type: int = TOUCH_APP_TYPE_MEDIA) -> bytearray:
    """Frame that wakes an already-configured touch surface."""
    return make_packet(
        CMD_TOUCH_CONTROL,
        bytearray([SUB_TOUCH, SUB_TOUCH_WAKE, app_type & 0xFF, TOUCH_STATE_AWAKE]),
    )


def parse_touch_config_reply(packet: bytearray) -> bool:
    """
    True if *packet* is the ring acknowledging a touch control frame.

    The acknowledgement is an echo: the ring answers a ``3B 02 …`` write by
    sending the same frame back on UART TX, confirmed in the Phase 0 probe.
    There is no status byte to read, so all this can meaningfully check is that
    the frame is a touch-control frame at all.
    """
    if len(packet) < 3 or packet[0] != CMD_TOUCH_CONTROL:
        return False
    return packet[1] == SUB_TOUCH and packet[2] in (
        SUB_TOUCH_CONFIG,
        SUB_TOUCH_OFF,
        SUB_TOUCH_WAKE,
    )


# What :func:`parse_touch_notice` returns for each recognised frame the ring
# pushes by itself, as the strings callers log and branch on.
TOUCH_NOTICE_OFF = "off"
TOUCH_NOTICE_SLEEP = "sleep"
TOUCH_NOTICE_WAKE = "wake"


def parse_touch_notice(packet: bytearray) -> str | None:
    """
    Classify an unsolicited ``0x3B`` frame the ring pushed on its own.

    Every ``3B 02`` *write* is echo-ACKed verbatim, so a frame that cannot be
    an echo - one that arrives while a touch session is open, with nothing of
    ours on the air - is the firmware telling us the touch state changed on
    the device. Three shapes are recognised:

    =========================================  ==========================
    ``3B 02 02 <appType> 00``                  surface slept: ``"sleep"``
    ``3B 02 02 <appType> 01``                  surface woke: ``"wake"``
    ``3B 02 01 00 00``                         touch disabled: ``"off"``
    =========================================  ==========================

    Sleep is *not* off. A configured surface idles after its sleep byte and
    wakes on the next touch, and the enable - and everything built on it -
    stays in place the whole time, so ``"sleep"``/``"wake"`` describe the
    surface, not the session, and callers must not read them as a disable.

    ``"off"`` is deliberately the *exact* shape of :func:`touch_off_packet`.
    QRing's decompiled driver reads ``02 01 <appType> <strength>`` as a
    gesture-mode write rather than a plain touch-off, so only the all-zero
    tail - the one shape both readings agree disables touch - classifies as
    ``"off"``; an ``02 01`` with any other tail returns ``None`` so a caller
    can log it as unknown instead of acting on it. ``02 00`` is the config
    frame, always the echo of our own enable, and also ``None``.

    Unlike upstream, no firmware has been *observed* sending these; the
    classification is what the OEM frames mean, so a caller can at least say
    accurately what it saw if a ring ever does push one.
    """
    if len(packet) < 5 or packet[0] != CMD_TOUCH_CONTROL or packet[1] != SUB_TOUCH:
        return None
    if packet[2] == SUB_TOUCH_OFF:
        if packet[3] == 0 and packet[4] == 0:
            return TOUCH_NOTICE_OFF
        return None
    if packet[2] == SUB_TOUCH_WAKE:
        if packet[4] == TOUCH_STATE_ASLEEP:
            return TOUCH_NOTICE_SLEEP
        if packet[4] == TOUCH_STATE_AWAKE:
            return TOUCH_NOTICE_WAKE
    return None


# ---------------------------------------------------------------------------
# The HID Consumer Control report
# ---------------------------------------------------------------------------

#: Exact payload length of a touch report: 18 declared one-bit fields, rounded
#: up to whole bytes. Not a Colmi frame, so not 16 bytes.
TOUCH_REPORT_LEN = 3

#: Report Reference (GATT descriptor ``2908``) of the touch report, as measured
#: on the R08: report ID 4, report type 1 (Input). The ring publishes four
#: ``2a4d`` Report declarations under one UUID, so this pair - not the UUID -
#: is what names the touch report. See ``api.resolve_touch_report_char``.
TOUCH_REPORT_ID = 4
HID_REPORT_TYPE_INPUT = 1

#: Byte 0 bit index -> HID Consumer page usage name, in the order the Report
#: Map declares them. Kept as usage names rather than gesture names because the
#: usage is the measured fact and the gesture is an interpretation of it.
TOUCH_USAGE_BITS: tuple[tuple[int, str], ...] = (
    (0x00, "scan_next"),  # 0x0B5
    (0x01, "scan_previous"),  # 0x0B6
    (0x02, "stop"),  # 0x0B7
    (0x03, "play_pause"),  # 0x0CD
    (0x04, "mute"),  # 0x0E2
    (0x05, "power"),  # 0x030
    (0x06, "menu"),  # 0x0E5
    (0x07, "select"),  # 0x0E7
    (0x08, "ac_home"),  # 0x0223
    (0x09, "ac_back"),  # 0x0224
    (0x0A, "ac_forward"),  # 0x0225
    (0x0B, "ac_refresh"),  # 0x0227
    (0x0C, "ac_previous"),  # 0x024A
    (0x0D, "ac_next"),  # 0x0225 (adjacent usages beyond 0x0B are decode-only)
    (0x0E, "ac_bookmarks"),  # 0x018A
    (0x0F, "browser_back"),
    (0x10, "browser_forward"),
    (0x11, "volume_up"),
    (0x12, "volume_down"),
    (0x13, "eject"),
    (0x14, "ac_home_search"),
    (0x15, "ac_search"),
    (0x16, "ac_navigate"),
    (0x17, "al_email_reader"),
    (0x18, "ac_zoom"),
)

# Gesture keys. These are entity unique-id suffixes and the ``gesture`` value in
# the published event data, so renaming one is a breaking change for any
# automation keyed to it.
GESTURE_TAP = "tap"
GESTURE_SWIPE_UP = "swipe_up"
GESTURE_SWIPE_DOWN = "swipe_down"

#: Usage -> gesture key. Only usages we have actually tied to a physical action
#: appear here, so an unobserved bit stays decodable (in ``TouchEvent.usages``)
#: without becoming an entity that can never fire.
#:
#: What belongs to whom: the usage names are the HID standard's, which physical
#: swipe lights which usage is the OEM firmware's choice, and "up"/"down" is
#: this integration's convention - so only measurement can pin the last link,
#: and it has been: a labelled on-device test (2026-11-14, R08 worn with the
#: sensor at the pad-side base of the finger) reported ``02 00 00``
#: (Scan Previous) for a physical swipe up and ``01 00 00`` (Scan Next) for a
#: swipe down. The original mapping guessed the other way, from Access
#: Bridge's ``GestureLogic.classify`` reading up == forward through a list.
#:
#: ``play_pause`` = tap was solid from the start: the probe's tester pressed
#: the button 15 times and every press reported bit ``0.3``.
USAGE_GESTURES: dict[str, str] = {
    "play_pause": GESTURE_TAP,
    "scan_next": GESTURE_SWIPE_DOWN,
    "scan_previous": GESTURE_SWIPE_UP,
}

#: Set back to ``True`` if a firmware or wear orientation is ever reported
#: that disagrees with the measured mapping above; callers warn on a non-empty
#: :data:`UNVERIFIED_GESTURES` instead of trusting the table blindly, and
#: ``scripts/_r08_check.py`` asserts the two still agree.
SWIPE_DIRECTION_UNVERIFIED = False

#: Gestures whose mapping still needs the labelled run, for callers (and the
#: selftest) that want to warn rather than silently offer wrong entities.
UNVERIFIED_GESTURES: frozenset[str] = (
    frozenset({GESTURE_SWIPE_UP, GESTURE_SWIPE_DOWN})
    if SWIPE_DIRECTION_UNVERIFIED
    else frozenset()
)


@dataclass(frozen=True, slots=True)
class TouchEvent:
    """
    One non-empty touch report, decoded.

    ``usages`` carries every bit that was set, so a usage we have no gesture
    for is still visible in the log instead of being silently dropped, and a
    future combo (two bits at once) cannot be mistranslated into a single
    gesture. ``gestures`` is the usable projection of that onto the gesture
    keys this integration offers.

    ``tap_count`` is always 1 on a freshly decoded report: the ring reports
    every press on its own, so a raw report is one tap by definition. It is
    only ever greater than 1 on an event ``TouchManager`` has coalesced from a
    multi-tap chain (``dataclasses.replace`` on the pending report when the
    chain's window lapses), and it rides along in the event data so an
    automation can branch on single vs. double vs. triple tap.
    """

    usages: frozenset[str]
    gestures: frozenset[str]
    raw: int
    tap_count: int = 1

    def as_event_data(self) -> dict[str, Any]:
        """Flat payload for a Home Assistant event."""
        return {
            "gesture": min(self.gestures, default=None),
            "gestures": sorted(self.gestures),
            "usages": sorted(self.usages),
            "raw": f"0x{self.raw:06X}",
            "tap_count": self.tap_count,
            "source": "touch",
        }


def parse_touch_report(packet: bytes | bytearray) -> TouchEvent | None:
    """
    Decode one Consumer Control report, or ``None`` if it reports nothing.

    The bitmap is little-endian within byte 0 as declared: bit ``0.0`` is
    ``Scan Next``, so ``packet[0] & (1 << n)`` selects entry ``n`` of
    :data:`TOUCH_USAGE_BITS`. All-zero means release - the ring sends one
    release after every press (25 releases for 25 presses in the capture),
    which is transport housekeeping and not a gesture, so it decodes to
    ``None`` rather than an event with no gestures.

    Wrong-length input is also ``None``. A notify subscription on a HID
    characteristic can legally receive anything the firmware decides to put
    there, and this runs on a notification callback; returning ``None`` lets
    the caller log it instead of raising inside the callback and killing
    notifications for the rest of the link.
    """
    if len(packet) != TOUCH_REPORT_LEN:
        return None
    value = packet[0] | (packet[1] << 8) | (packet[2] << 16)
    if value == 0:
        return None
    usages = frozenset(usage for bit, usage in TOUCH_USAGE_BITS if value & (1 << bit))
    gestures = frozenset(
        USAGE_GESTURES[usage] for usage in usages if usage in USAGE_GESTURES
    )
    return TouchEvent(usages=usages, gestures=gestures, raw=value)
