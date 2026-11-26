"""
Colmi daily skin-temperature history, big-data type ``0x25``.

The temperature log rides the "big-data" channel (see :mod:`big_data`), not the
16-byte command channel: a request is a single "days ago" byte wrapped in a
``bc 25`` frame, and the reply is one 50-byte *block* per requested day:

    ``<days ago> <slot minutes = 0x1e> <48 raw values>``

Each of the 48 values is one 30-minute slot of that UTC day, and the ring
encodes it as ``degC * 10 - 200`` - i.e. ``degC = raw / 10 + 20`` - with a raw
value of ``0`` meaning "no sample" (the ring only samples when worn and its
temperature monitoring is on). Verified on a real R09 (protocol doc, 2026-11):
a raw ``0xa9`` (169) in slot 29 decodes to 36.9 C at 14:30 UTC, and the slot
step byte reads ``0x1e`` (30).

The request byte selects the day: ``0`` returns *today's* (current UTC day)
block, while ``N >= 1`` returns completed days and, per the vendor app and the
ring's behaviour, omits the current day. Reading the day we are in therefore
means requesting ``0``. A ring that has never sampled answers with a short
``00 1e`` payload (no full block), which parses to no logs.

Like the stress and HRV day logs, the block carries no timestamps of its own
beyond the ``days ago`` offset, so slots are anchored to UTC midnight of the
offset day (the integration syncs the ring clock to UTC before reading).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import big_data, date_utils
from .packet import make_packet

logger = logging.getLogger(__name__)

# One block per day, laid out as [days ago][slot minutes][48 raw values].
BLOCK_LEN = 50
SLOTS_PER_DAY = 48
# Fallback slot step when the block's step byte is implausible; the ring has
# only ever been observed sending 30.
DEFAULT_SLOT_MINUTES = 30
MAX_SLOT_MINUTES = 120

# The ring's temperature encoding: degC = raw / SCALE + OFFSET.
TEMP_SCALE = 10.0
TEMP_OFFSET = 20.0

# Request payloads: today's block, versus completed days.
REQUEST_TODAY = 0


def read_temperature_packet(days_ago: int = REQUEST_TODAY) -> bytearray:
    """
    Build the ``bc 25`` request for a day ``days_ago`` before today.

    The big-data payload is the single day-offset byte; ``0`` asks for the
    current UTC day (the only one that yields today's in-progress block).
    """
    return big_data.build_request(
        big_data.TYPE_TEMPERATURE, bytearray([days_ago & 0xFF])
    )


def raw_to_degc(raw: int) -> float:
    """Convert one raw slot value to degrees Celsius."""
    return raw / TEMP_SCALE + TEMP_OFFSET


@dataclass
class TemperatureLog:
    """
    Skin-temperature raw values for one UTC day, one per 30-minute slot.

    ``values`` holds the ring's raw bytes (0 meaning no sample), kept raw so a
    consumer can tell a genuine reading from an absent one; ``timestamp`` is
    the start of the day the slots begin at (UTC).
    """

    values: list[int]
    timestamp: datetime
    slot_minutes: int = DEFAULT_SLOT_MINUTES

    def samples_with_times(self) -> list[tuple[float, datetime]]:
        """Non-zero (degC, start-of-slot time) pairs, oldest first."""
        step = timedelta(minutes=self.slot_minutes)
        return [
            (raw_to_degc(raw), self.timestamp + i * step)
            for i, raw in enumerate(self.values)
            if raw
        ]


def parse_temperature_blocks(payload: bytes | bytearray) -> list[TemperatureLog]:
    """
    Decode a ``bc 25`` reply payload into one :class:`TemperatureLog` per day.

    The payload is a run of :data:`BLOCK_LEN`-byte blocks; a short payload
    (the ring's ``00 1e`` "nothing recorded" answer) yields an empty list.
    Each block's ``days ago`` byte rebases its slots to the right UTC day.
    """
    logs: list[TemperatureLog] = []
    if len(payload) < BLOCK_LEN:
        logger.debug("temperature reply carries no full block (%d bytes)", len(payload))
        return logs
    start_of_today = date_utils.start_of_day(date_utils.now())
    for offset in range(0, len(payload) - BLOCK_LEN + 1, BLOCK_LEN):
        block = payload[offset : offset + BLOCK_LEN]
        days_ago = block[0]
        slot_minutes = block[1]
        if not 1 <= slot_minutes <= MAX_SLOT_MINUTES:
            slot_minutes = DEFAULT_SLOT_MINUTES
        logs.append(
            TemperatureLog(
                values=list(block[2 : 2 + SLOTS_PER_DAY]),
                timestamp=start_of_today - timedelta(days=days_ago),
                slot_minutes=slot_minutes,
            )
        )
    return logs


# All-day temperature monitoring preference, command 0x3A: the toggle that
# decides whether the ring samples skin temperature between syncs at all (and
# so whether any 0x25 history exists). It is a normal 16-byte command, not a
# big-data frame, and unlike its SpO2/stress/HRV siblings it carries an extra
# ``03`` sub-type before the read/write action: read ``3a 03 01``, write
# ``3a 03 02 <0|1>``. Verified on a real R09 (protocol doc, 2026-11): the
# enabled flag is in answer byte 3 (the ``03`` shifts it one past the other
# monitors' byte 2), e.g. ``3a 03 01 01`` after turning it on.
CMD_AUTO_TEMP_PREF = 58  # 0x3A
PREF_SUBTYPE = 0x03
PREF_READ = 0x01
PREF_WRITE = 0x02


def read_temp_pref_packet() -> bytearray:
    """Ask the ring whether all-day temperature monitoring is enabled."""
    return make_packet(CMD_AUTO_TEMP_PREF, bytearray([PREF_SUBTYPE, PREF_READ]))


def write_temp_pref_packet(enabled: bool) -> bytearray:
    """Turn the ring's all-day temperature monitoring (and its history) on/off."""
    return make_packet(
        CMD_AUTO_TEMP_PREF,
        bytearray([PREF_SUBTYPE, PREF_WRITE, 0x01 if enabled else 0x00]),
    )


def parse_temp_pref_reply(packet: bytearray) -> bool:
    """
    Read the enable byte of a 0x3A answer (read reply or write echo).

    Byte 3 == 0x01 means on (the ``03`` sub-type shifts the flag one past the
    other auto-monitor commands' byte 2), as the field-tested R09 frames show.
    """
    raw = packet[3]
    if raw not in (0x00, 0x01):
        logger.warning(
            "Unexpected value in temperature preference byte %s, defaulting to false",
            raw,
        )
    return raw == 0x01
