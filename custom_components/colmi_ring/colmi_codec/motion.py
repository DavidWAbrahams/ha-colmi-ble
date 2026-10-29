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
  disabled with ``02 06``.

*Unverified on our R02.* Parity with a web demo for a different ring is not the
same as the R02 firmware agreeing, and two things go beyond anything the
reference states: which ``packet[1]`` values are a camera-gesture *report* (see
:data:`GESTURE_REPORT_TYPES`), and whether accel data really arrives on
``CMD_RAW_MODE`` at all - the reference classifies notifications by ``packet[1]``
alone and listens to every notifiable characteristic, so it never commits to a
command byte. Those are the Phase 0 probe's to settle (:mod:`scripts._accel_probe`),
and they are deliberately isolated in this module and in the ``MOTION_*``
constants in ``const.py`` so a correction lands in one place without touching the
detector or the Home Assistant surface.

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

# ``packet[1]`` values treated as "the on-device gesture just fired". The
# reference only documents the on/off *commands*, so the report codes are a
# guess; anything on ``CMD_CAMERA_GESTURE`` that is not an obvious command echo
# is reported. See ``MotionManager`` for the startup mute that keeps the ring
# echoing our own enable packet from producing a phantom clap.
GESTURE_REPORT_TYPES = frozenset({4, 5, 7})

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
