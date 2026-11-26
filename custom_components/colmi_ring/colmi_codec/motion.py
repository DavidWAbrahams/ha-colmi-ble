"""
Accelerometer (RAW motion) and on-device gesture packets.

Protocol reference: ``mrfloydst/smartringmidi`` ``bluetooth.html``, whose axis
bit-packing is itself derived from ``atc1441/ATC_RF03_Ring``.

Two different kinds of assumption live here, and it is worth telling them apart.

*Agrees with the reference.* The frame layouts and the decode below were compared
against a literal transcription of the reference's JavaScript - command bytes and
checksum, and the axis decode over the whole 12-bit space - with no mismatches.
:mod:`scripts._motion_ref_parity` re-checks that in a second, without a ring:

* RAW mode is enabled with ``A1 04 04`` and disabled with ``A1 02``.
* A position notification is identified by ``packet[1] == 3``.
* Each axis is a 12-bit big-endian signed value split over two bytes, in wire
  order **Y, Z, X**, with the low bits in the *low* nibble of the next byte.
* The range is +/-4 g, so ``g = (raw / 2048) * 4``.
* The ring's on-device "camera button" gesture is enabled with ``02 04`` and
  disabled with ``02 06``, and it works on its own: no RAW stream is needed, and
  the setting survives a disconnect.

*Unverified on our R02.* Whether accel data really arrives on ``CMD_RAW_MODE`` at
all is still an assumption - the reference classifies notifications by
``packet[1]`` alone and listens to every notifiable characteristic, so it never
commits to a command byte. That one belongs to the Phase 0 probe
(:mod:`scripts._accel_probe`) and is isolated in this module and in the
``MOTION_*`` constants in ``const.py`` so a correction lands in one place without
touching the detector or the Home Assistant surface.

*Measured on our R02.* The camera-gesture *report* byte is no longer a guess; see
:data:`GESTURE_REPORT_TYPES`. Two facts came out of the same capture, via the
ATC RF03 Writer web tool, and both matter to ``MotionManager``: a wave yields one
packet only while a central is actually connected, and the ring does not echo the
``02 04`` enable back at us.

Like the rest of this subpackage this module is pure: no Home Assistant, no
Bluetooth, no allocation on the hot path beyond the sample dataclass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

from .packet import make_packet

# Command bytes.
CMD_RAW_MODE = 0xA1
CMD_CAMERA_GESTURE = 0x02

# Sub-command (``packet[1]``) payloads.
SUB_RAW_ON = 4
SUB_RAW_OFF = 2
SUB_GESTURE_ON = 4
SUB_GESTURE_OFF = 6

# ``packet[1]`` value that marks a notification as position/accelerometer data.
TYPE_POSITION = 3

# ``packet[1]`` value that means "the on-device gesture just fired".
#
# Measured on an R02 with the ATC RF03 Writer web tool: ``02 04`` (checksum 06)
# starts camera mode on its own, with no RAW stream, and each wave then reports
# exactly one 16-byte packet, ``02 02 00 00 00 00 00 00 00 00 00 00 00 00 00 04``
# (checksum 04, well-formed, all-zero body). Subtype 2 is therefore the report.
#
# Note 2 is also ``SUB_RAW_OFF``: on ``CMD_CAMERA_GESTURE`` it is a wave, and the
# command bytes keep the two apart. Nothing was seen echoed back after the
# ``02 04`` write, so this set deliberately excludes 4 - accepting our own enable
# as a report is what the old guess ({4, 5, 7}) got wrong in both directions: it
# could not see real waves, and it could invent them.
GESTURE_REPORT_TYPES = frozenset({2})

# 12-bit signed accelerometer payload, scaled to +/-4 g.
RAW_MAX = 2048
G_FULL_SCALE = 4.0


class Gesture(IntEnum):
    """A gesture recognised by the ring itself."""

    CAMERA = 1


def get_raw_start_packet() -> bytearray:
    """Enable the ring's RAW accelerometer stream."""
    return make_packet(CMD_RAW_MODE, bytearray([SUB_RAW_ON, SUB_RAW_ON]))


def get_raw_stop_packet() -> bytearray:
    """Disable the RAW stream and return the ring to normal reporting."""
    return make_packet(CMD_RAW_MODE, bytearray([SUB_RAW_OFF]))


def get_camera_gesture_on_packet() -> bytearray:
    """Enable the ring's on-device camera-button (wave) gesture detection."""
    return make_packet(CMD_CAMERA_GESTURE, bytearray([SUB_GESTURE_ON]))


def get_camera_gesture_off_packet() -> bytearray:
    """Disable the on-device camera-button gesture detection."""
    return make_packet(CMD_CAMERA_GESTURE, bytearray([SUB_GESTURE_OFF]))


@dataclass(frozen=True, slots=True)
class AccelSample:
    """One accelerometer reading, in units of g, in the ring's own frame."""

    x: float
    y: float
    z: float

    @property
    def magnitude_g(self) -> float:
        """Total acceleration magnitude including gravity."""
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def dot(self, other: AccelSample) -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def minus(self, other: AccelSample) -> AccelSample:
        return AccelSample(self.x - other.x, self.y - other.y, self.z - other.z)

    def scale(self, factor: float) -> AccelSample:
        return AccelSample(self.x * factor, self.y * factor, self.z * factor)


def _int12(value: int) -> int:
    """Sign-extend a 12-bit two's-complement value."""
    return value - 4096 if value > 2047 else value


def _axis(high: int, low: int) -> float:
    """Decode one axis from a high byte plus the low bits of the next byte."""
    raw = ((high << 4) | (low & 0xF)) & 0xFFF
    return (_int12(raw) / RAW_MAX) * G_FULL_SCALE


def parse_accel_packet(packet: bytearray) -> AccelSample | None:
    """
    Return the accelerometer sample in ``packet``, or ``None``.

    ``None`` means "not a position packet" (or too short to decode). Returning
    instead of raising matters here: unlike the single-shot parsers, this runs
    on every notification of an open-ended stream, where one unexpected frame
    must not tear the session down.
    """
    if len(packet) < 8 or packet[1] != TYPE_POSITION:
        return None
    # Wire order is Y, Z, X.
    return AccelSample(
        x=_axis(packet[6], packet[7]),
        y=_axis(packet[2], packet[3]),
        z=_axis(packet[4], packet[5]),
    )


def parse_gesture_packet(packet: bytearray) -> Gesture | None:
    """Return the on-device gesture announced by ``packet``, or ``None``."""
    if len(packet) < 2 or packet[0] != CMD_CAMERA_GESTURE:
        return None
    if packet[1] not in GESTURE_REPORT_TYPES:
        return None
    return Gesture.CAMERA


# --- Timed raw stream (0x69 type 13, R09 family) -----------------------------
#
# Newer Colmi firmware (verified on an R09, FW RT09CR2_3.00.03_260929, per the
# ring0 protocol notes of 2026-11-11) answers the real-time command with a
# *timed* raw sensor session: ``69 0D <duration u16 LE>`` streams fourteen
# payload bytes at about 25 Hz for ``duration`` seconds, then the ring stops
# by itself. The payload carries two optical channels and three s16 LE
# accelerometer axes - the same information RAW mode carries, but on a
# command whose stop this firmware actually honours (see
# :func:`get_realtime_stream_stop_packet`), where the 0xA1 path latches
# (field log 2026-11-25).
#
# Type 14 is the same session on the SpO2 LED pair and is deliberately not
# used: its red sensor light survives even the verified stop.
#
# The accelerometer's unit is not documented. At-rest captures show the
# three values "small and near zero", which pins down nothing useful, so
# ``REALTIME_ACCEL_LSB_PER_G`` stays at 1.0 (raw units) until a field
# capture pins the scale; ``MotionManager._async_check_rate`` logs the
# at-rest median magnitude, and that figure is what calibrates this.

#: Command byte of the real-time session channel - the same byte the
#: heart-rate/SpO2 real-time codec listens on (see ``real_time.py``), so a
#: stream listener on it must filter by session type.
CMD_REALTIME_STREAM = 0x69

#: Session type (``packet[1]``) whose packets carry the timed raw stream.
REALTIME_STREAM_TYPE = 0x0D

#: The stop for a type-13 session is not ``6A 0D 00 00`` (measured not to
#: work): starting any other real-time type shuts the previous session's
#: sensor and timer down, so the stop is "start type 6, action stop". This
#: payload was verified on the ring on 2026-11-11.
_REALTIME_STOP_PAYLOAD = bytearray([0x06, 0x04])

#: The firmware rejects durations below 10 s, and its 500 ms session counter
#: is 16-bit, so durations saturate at 65535 s.
REALTIME_STREAM_MIN_DURATION_S = 10
REALTIME_STREAM_MAX_DURATION_S = 65535

#: Division applied to the raw s16 axes to turn them into g. Unknown until
#: calibrated from a field capture; 1.0 keeps samples in raw units, and the
#: at-rest magnitude that ``MotionManager`` logs names the number to set
#: here (a ring at rest must read a median magnitude of ~1 g).
REALTIME_ACCEL_LSB_PER_G = 1.0


def get_realtime_stream_start_packet(duration_s: int) -> bytearray:
    """
    Start the R09-family timed raw stream, self-stopping after ``duration_s``.

    The duration is clamped into the range the firmware itself enforces
    (10..65535 s). It is a failsafe rather than the session's intended life:
    the integration always sends the stop frame first, and the ring's
    self-stop only matters when that frame never reaches it.
    """
    duration = max(
        REALTIME_STREAM_MIN_DURATION_S,
        min(int(duration_s), REALTIME_STREAM_MAX_DURATION_S),
    )
    return make_packet(
        CMD_REALTIME_STREAM,
        bytearray([REALTIME_STREAM_TYPE, duration & 0xFF, duration >> 8]),
    )


def get_realtime_stream_stop_packet() -> bytearray:
    """
    Stop the timed raw stream by starting (and stopping) another session.

    ``6A 0D 00 00`` leaves a type-13 session streaming; starting any other
    real-time type shuts the previous session's sensor and timer down, so
    the stop is "start type 6, action stop" - the frame verified on the
    ring on 2026-11-11, after the plain stop frame was measured not to work.
    """
    return make_packet(CMD_REALTIME_STREAM, bytearray(_REALTIME_STOP_PAYLOAD))


def _s16le(packet: bytearray, offset: int) -> int:
    """Decode the little-endian signed 16-bit value starting at ``offset``."""
    value = packet[offset] | (packet[offset + 1] << 8)
    return value - 0x10000 if value > 0x7FFF else value


def parse_realtime_accel_packet(packet: bytearray) -> AccelSample | None:
    """
    Return the accelerometer sample in a type-13 stream packet, or ``None``.

    Payload layout (payload index ``n`` is ``packet[1 + n]``): ``[0]=type
    [1]=error [2]=sequence (wraps at 256) [3]=status/gain [4..5]=u16 LE
    optical A [6..7]=u16 LE optical B [8..9], [10..11], [12..13]=s16 LE
    accelerometer``. Which physical axis each of the three carries is not
    documented, so they are named x, y, z in wire order; like the RAW
    stream's axis mapping, the hole-axis option is the calibration hook.
    The error byte is not consulted: nonzero values have never been
    observed, and dropping samples on an unverified assumption would starve
    the gesture detector on a stream where roughly 4.5% of packets are
    already lost.
    """
    if (
        len(packet) < 15
        or packet[0] != CMD_REALTIME_STREAM
        or packet[1] != REALTIME_STREAM_TYPE
    ):
        return None
    scale = REALTIME_ACCEL_LSB_PER_G
    return AccelSample(
        x=_s16le(packet, 9) / scale,
        y=_s16le(packet, 11) / scale,
        z=_s16le(packet, 13) / scale,
    )
