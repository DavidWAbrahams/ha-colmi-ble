"""Accelerometer (RAW motion) and on-device gesture packets.

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
ATC RF03 Writer web tool, and both matter to ``MotionManager``: a clap yields one
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
# starts camera mode on its own, with no RAW stream, and each clap then reports
# exactly one 16-byte packet, ``02 02 00 00 00 00 00 00 00 00 00 00 00 00 00 04``
# (checksum 04, well-formed, all-zero body). Subtype 2 is therefore the report.
#
# Note 2 is also ``SUB_RAW_OFF``: on ``CMD_CAMERA_GESTURE`` it is a clap, and the
# command bytes keep the two apart. Nothing was seen echoed back after the
# ``02 04`` write, so this set deliberately excludes 4 - accepting our own enable
# as a report is what the old guess ({4, 5, 7}) got wrong in both directions: it
# could not see real claps, and it could invent them.
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
    """Enable the ring's on-device camera-button (clap) gesture detection."""
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
    """Return the accelerometer sample in ``packet``, or ``None``.

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
