"""
Unsolicited notifications the ring pushes on its own initiative.

Command byte 0x73 is the notification channel. Unlike every other command in
this codec, frames here are *announcements*, not replies: the ring sends one
whenever something it considers worth saying changes, and the second byte
picks which kind of announcement. Observed sub-commands:

    0x01  new heart-rate data available  (doorbell: no values in the frame)
    0x03  new SpO2 data available        (doorbell: no values in the frame)
    0x04  new step data available        (doorbell: no values in the frame)
    0x0C  battery report                 (level + charging, inline)
    0x12  live activity                  (steps, calories, distance, inline)

The doorbell frames carry no metric values at all; they only say the ring has
logged something new, to be fetched later with the usual read commands.
"""

from dataclasses import dataclass

from . import battery

CMD_NOTIFICATION = 0x73

SUB_NEW_HR = 0x01
SUB_NEW_SPO2 = 0x03
SUB_NEW_STEPS = 0x04
SUB_BATTERY = 0x0C
SUB_LIVE_ACTIVITY = 0x12


@dataclass
class ActivityInfo:
    steps: int
    calories_raw: int
    distance_m: int


def notification_sub(packet: bytes | bytearray) -> int | None:
    """
    The sub-command byte of a notification frame, or None if not one.

    Dispatch aid: the caller picks a behaviour from the sub-command before
    parsing any payload, so a frame carrying a value the decoder has never
    seen still counts as "interpreted traffic" and not a surprise.
    """
    if len(packet) < 2 or packet[0] != CMD_NOTIFICATION:
        return None
    return packet[1]


def parse_battery_push(packet: bytes | bytearray) -> battery.BatteryInfo | None:
    r"""
    Decode the ring's unsolicited battery report, if this frame is one.

    The payload layout matches the polled reply's: byte 1 is the level, byte 2
    the charging flag - but the fields sit two bytes later because the frame
    carries its own opcode and sub-command first. Observed on an R08 as
    ``73 0c 64 01`` (100 %, charging) with the rest of the 16-byte frame zero
    apart from the trailing checksum.

    Returns None for anything that is not a battery push: 0x73 is a
    notification channel, and a frame with an unrecognised sub-command is not
    evidence of a battery, whatever else the ring decides to announce there.
    """
    if len(packet) < 4 or packet[0] != CMD_NOTIFICATION or packet[1] != SUB_BATTERY:
        return None
    return battery.BatteryInfo(battery_level=packet[2], charging=bool(packet[3]))


def parse_live_activity(packet: bytes | bytearray) -> ActivityInfo | None:
    r"""
    Decode the ring's live activity report, if this frame is one.

    The three day counters are 24-bit little-endian fields: bytes 2..4 are
    the step count, bytes 5..7 the calories and bytes 8..10 the distance in
    metres. Unlike the doorbell sub-commands, this one carries its values
    inline, so it needs no follow-up poll.

    The calorie field is returned *raw*. This integration's verified
    convention (see ``api.ColmiRingApiClient.async_get_data``) is that a
    ring calorie field counts thousandths of a kilocalorie, but applying
    that scaling is the consumer's call, not the decoder's - the decoder's
    job is to reproduce the bytes.

    Returns None for anything that is not a live activity frame.
    """
    if (
        len(packet) < 11
        or packet[0] != CMD_NOTIFICATION
        or packet[1] != SUB_LIVE_ACTIVITY
    ):
        return None
    return ActivityInfo(
        steps=_u24le(packet, 2),
        calories_raw=_u24le(packet, 5),
        distance_m=_u24le(packet, 8),
    )


def _u24le(packet: bytes | bytearray, offset: int) -> int:
    """Little-endian 24-bit unsigned integer starting at *offset*."""
    return packet[offset] | (packet[offset + 1] << 8) | (packet[offset + 2] << 16)
