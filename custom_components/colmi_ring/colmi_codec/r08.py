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

.. warning::

   The swipe **direction** mapping below is unverified. See
   ``SWIPE_DIRECTION_UNVERIFIED``.
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

# The documented media-key profile: what the R08 Access Bridge pins, and what
# the Phase 0 probe captured touch reports under. appType 0..7 are accepted by
# the firmware; only 1 has been observed to produce reports.
TOUCH_APP_TYPE_MEDIA = 1
# Access Bridge's "Touch fallback" debug profile. Reported to work, never
# captured here, and not needed: the media profile already emits the reports.
TOUCH_APP_TYPE_TOUCH_FALLBACK = 4

# The OEM app's default touch sleep, in minutes (its ``touchMusicKeys`` frame
# sends 0x05). After this long the touch surface sleeps; whether notifications
# resume on the next tap or need a re-arm is an open question (plan Phase 0.5).
DEFAULT_TOUCH_SLEEP_MINUTES = 5

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
        bytearray([SUB_TOUCH, SUB_TOUCH_WAKE, app_type & 0xFF, 0x01]),
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
#: ``play_pause`` = tap is solid: the probe's tester pressed the button 15 times
#: and every press reported bit ``0.3``.
#:
#: The two swipes are NOT solid - see ``SWIPE_DIRECTION_UNVERIFIED``.
USAGE_GESTURES: dict[str, str] = {
    "play_pause": GESTURE_TAP,
    "scan_next": GESTURE_SWIPE_UP,
    "scan_previous": GESTURE_SWIPE_DOWN,
}

#: TODO(r08-touch): **swipe direction is unverified - confirm before shipping
#: the swipe entities.**
#:
#: The Phase 0 capture performed swipes but did not label them, so the tally
#: proves only that the two directions are *distinct*: 7 reports of
#: ``01 00 00`` (Scan Next) and 3 of ``02 00 00`` (Scan Previous), never both
#: bits at once. Which physical direction is which is a guess, currently
#: "Scan Next = up", taken from Access Bridge's ``GestureLogic.classify``
#: mapping Scan Next to FORWARD / Scan Previous to BACKWARD and reading the
#: glasses' launcher as up == forward through the list. That last step is
#: inference, not measurement.
#:
#: Nothing in this file should be edited to "fix" a reversed swipe except this
#: table and this comment: flip the two ``USAGE_GESTURES`` entries and the
#: entities follow. Settled by one labelled probe run (plan Phase 0.5), whose
#: ``testdata/r08/`` capture becomes the selftest fixture that pins the mapping.
SWIPE_DIRECTION_UNVERIFIED = True

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
    """

    usages: frozenset[str]
    gestures: frozenset[str]
    raw: int

    def as_event_data(self) -> dict[str, Any]:
        """Flat payload for a Home Assistant event."""
        return {
            "gesture": min(self.gestures, default=None),
            "gestures": sorted(self.gestures),
            "usages": sorted(self.usages),
            "raw": f"0x{self.raw:06X}",
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
