"""
Colmi daily HRV history, command 0x39.

When all-day HRV monitoring is enabled on the ring, it records one HRV value
(0 meaning "no sample") for each 30-minute slot of a day. Requesting a day's
log sends a little-endian int32 "days ago" count (0 = today) as the payload;
the reply is a burst of up to five 16-byte packets:

- byte 0 is always 57 (0x39), byte 1 is the packet number:
- packet 0xFF -> the ring has no HRV history for that day ("empty", sync
  aborted),
- packet 0x00 -> header (byte 2 = number of data packets that follow),
- packet 0x01 -> 12 values in bytes 3..14 (byte 2 is a start offset, observed
  as 0), covering slots 0..11,
- packets 0x02..0x04 -> 13 values each in bytes 2..14, continuing the slot
  sequence; packet 4 is always the last one.

That is 12 + 3 * 13 = 51 slots, enough for a 48-slot day (tail slots past
midnight are zero). This matches Gadgetbridge's field-tested
``ColmiR0xDeviceSupport.fetchHistoryHRV`` / ``ColmiR0xPacketHandler
.historicalHRV``, which walks days 0..6 one request at a time.

Like the stress log, the reply carries no timestamps, so a finished log is
anchored to midnight of the *current* UTC day (the integration syncs the ring
clock to UTC before reading); :meth:`HrvLog.day_shifted` rebases it for older
days.
"""

import logging
import struct
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from . import date_utils
from .packet import make_packet

CMD_SYNC_HRV = 57  # 0x39

# The all-day HRV monitoring preference, command 0x38: the toggle that decides
# whether the ring samples HRV (and therefore whether any 0x39 history exists)
# at all. Read with [0x01], written with [0x02, enable]; the answer's byte 2 is
# 0x01 when on (Gadgetbridge's field-tested ``hrvSettings`` treats both the
# read reply and the write echo this way). Unlike the 0x16 heart-rate logging
# settings there is no interval byte anywhere in the verified frames: the
# history grid is fixed at one slot per 30 minutes.
CMD_AUTO_HRV_PREF = 56  # 0x38
PREF_READ = 0x01
PREF_WRITE = 0x02

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

logger = logging.getLogger(__name__)


def read_hrv_pref_packet() -> bytearray:
    """Ask the ring whether all-day HRV monitoring is enabled."""
    return make_packet(CMD_AUTO_HRV_PREF, bytearray([PREF_READ]))


def write_hrv_pref_packet(enabled: bool) -> bytearray:
    """Turn the ring's all-day HRV monitoring (and its history) on or off."""
    return make_packet(
        CMD_AUTO_HRV_PREF, bytearray([PREF_WRITE, 0x01 if enabled else 0x00])
    )


def parse_hrv_pref_reply(packet: bytearray) -> bool:
    """
    Read the enable byte of a 0x38 answer (read reply or write echo).

    Byte 2 == 0x01 means on, exactly as Gadgetbridge's ``hrvSettings`` parses
    it for both kinds of reply.
    """
    raw = packet[2]
    if raw not in (0x00, 0x01):
        logger.warning(
            "Unexpected value in HRV preference byte %s, defaulting to false",
            raw,
        )
    return raw == 0x01


def read_hrv_packet(days_ago: int = 0) -> bytearray:
    """
    Request the HRV history for a day ``days_ago`` days before today.

    The payload is the day offset as a little-endian int32, exactly what
    Gadgetbridge's ``fetchHistoryHRV`` sends.
    """
    payload = bytearray(struct.pack("<L", days_ago))
    return make_packet(CMD_SYNC_HRV, payload)


@dataclass
class HrvLog:
    """
    HRV values for one day, one entry per 30-minute slot.

    ``values`` is always exactly ``SLOTS_PER_DAY`` long; 0 means no sample.
    ``timestamp`` is the start of the day (UTC) the slots begin at.
    """

    values: list[int]
    timestamp: datetime

    def day_shifted(self, days_ago: int) -> "HrvLog":
        """Rebase the slots to a day ``days_ago`` earlier (the request offset)."""
        if not days_ago:
            return self
        return replace(self, timestamp=self.timestamp - timedelta(days=days_ago))

    def samples_with_times(self) -> list[tuple[int, datetime]]:
        """Non-zero (value, start-of-slot time) pairs, oldest first."""
        step = timedelta(minutes=SLOT_MINUTES)
        return [
            (value, self.timestamp + i * step)
            for i, value in enumerate(self.values)
            if value
        ]


class NoData:
    """Returned when the ring reports an empty HRV history."""


class HrvLogParser:
    """Stateful per-transaction parser, like ``StressLogParser``."""

    def __init__(self) -> None:
        """Start with no burst in progress."""
        self.reset()

    def reset(self) -> None:
        """Drop any partial burst so the next one parses cleanly."""
        self._raw: list[int] = []
        self.size = 0

    def parse(self, packet: bytearray) -> HrvLog | NoData | None:
        r"""
        Feed one reply packet to the parser.

        First byte of the packet should always be CMD_SYNC_HRV (57);
        second byte is the packet number. Return the finished log on the
        final packet, NoData on an empty answer, None while collecting.

        example header: bytearray(b'\x39\x00\x04\x00\x00...\x00')
        (four data packets follow)
        """
        packet_nr = packet[1]
        if packet_nr == PACKET_EMPTY:
            logger.debug("HRV history request answered as empty")
            self.reset()
            return NoData()

        if packet_nr == PACKET_HEADER:
            # Header packet. Trust its shape but not its size: completion is
            # driven by the final packet number, as upstream does.
            self.reset()
            self.size = packet[2]
            return None

        if packet_nr == PACKET_FIRST_DATA:
            # 12 values start at byte 3 (byte 2 is a start offset, 0 observed).
            self._store(0, packet[3 : 3 + FIRST_PACKET_VALUES])
        elif packet_nr > PACKET_FIRST_DATA:
            base = FIRST_PACKET_VALUES + (packet_nr - 2) * LATER_PACKET_VALUES
            self._store(base, packet[2 : 2 + LATER_PACKET_VALUES])

        if packet_nr >= FINAL_PACKET_NR:
            result = HrvLog(
                values=self._values(),
                timestamp=date_utils.start_of_day(date_utils.now()),
            )
            self.reset()
            return result
        return None

    def _store(self, base: int, chunk: bytearray) -> None:
        """Write the packet's slot values at ``base``, zero-filling any gap."""
        if base > len(self._raw):
            self._raw.extend([0] * (base - len(self._raw)))
        self._raw[base : base + len(chunk)] = list(chunk)

    def _values(self) -> list[int]:
        """Pad or trim the raw slots to exactly one day's worth."""
        values = self._raw.copy()
        if len(values) > SLOTS_PER_DAY:
            values = values[:SLOTS_PER_DAY]
        elif len(values) < SLOTS_PER_DAY:
            values.extend([0] * (SLOTS_PER_DAY - len(values)))
        return values
