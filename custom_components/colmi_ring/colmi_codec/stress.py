"""
Colmi daily stress ("pressure") history, command 0x37.

The ring records one stress score (0-100, 0 meaning "no sample") for each
30-minute slot of the current day. Reading the log is a payload-free request;
the reply is a burst of up to five 16-byte packets:

- byte 0 is always 55 (0x37), byte 1 is the packet number:
- packet 0xFF -> the ring has no stress history ("empty", sync aborted),
- packet 0x00 -> header (byte 2 = number of data points, byte 3 = minutes per
  slot; observed as 30),
- packet 0x01 -> 12 values in bytes 3..14 (byte 2 is a start offset, observed
  as 0), covering slots 0..11,
- packets 0x02..0x04 -> 13 values each in bytes 2..14, continuing the slot
  sequence; packet 4 is always the last one.

That is 12 + 3 * 13 = 51 slots, enough for a 48-slot day (tail slots past
midnight are zero). This matches Gadgetbridge's field-tested
``ColmiR0xPacketHandler.historicalStress``.

Unlike the daily heart-rate log, the reply carries no timestamps, so slots are
anchored to midnight of the day the ring believes it is; this integration
syncs the ring clock to UTC before reading, so we anchor to UTC midnight.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import date_utils
from .packet import make_packet

CMD_READ_STRESS = 55  # 0x37, called "pressure" upstream

# Slots in a day, and the minutes each slot covers.
SLOTS_PER_DAY = 48
SLOT_MINUTES = 30

# Packet numbers (byte 1 of the reply).
PACKET_HEADER = 0x00
PACKET_FIRST_DATA = 0x01
PACKET_EMPTY = 0xFF
# Last packet of the burst (Gadgetbridge treats packet 4 as final).
FINAL_PACKET_NR = 4

# Slot value counts per data packet: packet 1 carries 12 (it spends a byte on
# a start offset), packets 2..4 carry 13.
FIRST_PACKET_VALUES = 12
LATER_PACKET_VALUES = 13
# Plausible bound for the header's "minutes per slot" field.
MAX_SLOT_MINUTES = 120

logger = logging.getLogger(__name__)


def read_stress_packet() -> bytearray:
    """Request today's stress history; the command takes no payload."""
    return make_packet(CMD_READ_STRESS)


@dataclass
class StressLog:
    """
    Stress values for one day, one entry per 30-minute slot.

    ``values`` is always exactly ``SLOTS_PER_DAY`` long; 0 means no sample.
    ``timestamp`` is the start of the day (UTC) the slots begin at.
    """

    values: list[int]
    timestamp: datetime

    def samples_with_times(self) -> list[tuple[int, datetime]]:
        """Non-zero (value, start-of-slot time) pairs, oldest first."""
        step = timedelta(minutes=SLOT_MINUTES)
        return [
            (value, self.timestamp + i * step)
            for i, value in enumerate(self.values)
            if value
        ]


class NoData:
    """Returned when the ring reports an empty stress history."""


class StressLogParser:
    """Stateful per-transaction parser, like ``hr.HeartRateLogParser``."""

    def __init__(self) -> None:
        """Start with no burst in progress."""
        self.reset()

    def reset(self) -> None:
        """Drop any partial burst so the next one parses cleanly."""
        self._raw: list[int] = []
        self.size = 0
        self.range = SLOT_MINUTES

    def parse(self, packet: bytearray) -> StressLog | NoData | None:
        r"""
        Feed one reply packet to the parser.

        First byte of the packet should always be CMD_READ_STRESS (55);
        second byte is the packet number. Return the finished log on the
        final packet, NoData on an empty answer, None while collecting.

        example header: bytearray(b'\x37\x00\x33\x1e\x00...\x00')
        (51 points, 30-minute slots)
        """
        packet_nr = packet[1]
        if packet_nr == PACKET_EMPTY:
            logger.debug("stress log request answered as empty")
            self.reset()
            return NoData()

        if packet_nr == PACKET_HEADER:
            # Header packet. Trust its shape but not its size: completion is
            # driven by the final packet number, as upstream does.
            self.reset()
            self.size = packet[2]
            slot_range = packet[3]
            if 1 <= slot_range <= MAX_SLOT_MINUTES:
                self.range = slot_range
            return None

        if packet_nr == PACKET_FIRST_DATA:
            # 12 values start at byte 3 (byte 2 is a start offset, 0 today).
            self._store(0, packet[3 : 3 + FIRST_PACKET_VALUES])
        elif packet_nr > PACKET_FIRST_DATA:
            base = FIRST_PACKET_VALUES + (packet_nr - 2) * LATER_PACKET_VALUES
            self._store(base, packet[2 : 2 + LATER_PACKET_VALUES])

        if packet_nr >= FINAL_PACKET_NR:
            result = StressLog(
                values=self._values(),
                timestamp=date_utils.start_of_day(date_utils.now()),
            )
            self.reset()
            return result
        return None

    def _store(self, base: int, chunk: bytearray) -> None:
        """Write 13 slot values at ``base``, zero-filling any gap first."""
        if base > len(self._raw):
            self._raw.extend([0] * (base - len(self._raw)))
        self._raw[base : base + 13] = list(chunk)

    def _values(self) -> list[int]:
        """Pad or trim the raw slots to exactly one day's worth."""
        values = self._raw.copy()
        if len(values) > SLOTS_PER_DAY:
            values = values[:SLOTS_PER_DAY]
        elif len(values) < SLOTS_PER_DAY:
            values.extend([0] * (SLOTS_PER_DAY - len(values)))
        return values
