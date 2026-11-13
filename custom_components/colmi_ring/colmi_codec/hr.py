"""This is called the DailyHeartRate in Java."""

import logging
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import date_utils
from .packet import make_packet

CMD_READ_HEART_RATE = 21  # 0x15

logger = logging.getLogger(__name__)


def read_heart_rate_packet(target: datetime) -> bytearray:
    """Target datetime should be at midnight for the day of interest"""
    data = bytearray(struct.pack("<L", int(target.timestamp())))

    return make_packet(CMD_READ_HEART_RATE, data)


def _add_times(
    heart_rates: list[int], ts: datetime, range_minutes: int
) -> list[tuple[int, datetime]]:
    """
    Pair each slot with the time it covers, stepping by the ring's slot grid.

    The grid is whatever the ring declared in the log header (byte 3), NOT a
    fixed 5 minutes: the R08 (per the R08-Access-Bridge HeartAssembler and
    QRing) reports its *sampling interval* as the grid, so a 10-min ring
    fills one slot per 10 minutes. Stepping it by a wrong 5 minutes stamps
    every sample too far in the past (a 14:00 read as 07:00 on a 10-min
    ring), which the coordinator's staleness check then treats as dead.
    """
    assert range_minutes >= 1, "Slot grid must be at least a minute"
    result = []
    m = datetime(ts.year, ts.month, ts.day, tzinfo=ts.tzinfo)
    step = timedelta(minutes=range_minutes)
    for hr in heart_rates:
        result.append((hr, m))
        m += step

    return result


@dataclass
class HeartRateLog:
    heart_rates: list[int]
    timestamp: datetime
    size: int
    index: int
    range: int
    """Slot grid in minutes, as reported by the ring in the log header."""

    def heart_rates_with_times(self):
        return _add_times(self.heart_rates, self.timestamp, self.range)


class NoData:
    """Returned when there's no heart rate data"""


class HeartRateLogParser:
    # Upper bound for a sane slot grid (a ring that logs once a day at worst).
    MAX_RANGE_MINUTES = 1440

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._raw_heart_rates: list[int] = []
        self.timestamp: datetime | None = None
        self.size = 0
        self.index = 0
        self.end = False
        self.range = 5

    def is_today(self) -> bool:
        d = self.timestamp
        if d is None:
            return False
        return date_utils.is_today(d)

    def parse(self, packet: bytearray) -> HeartRateLog | NoData | None:
        r"""
        First byte of packet should always be CMD_READ_HEART_RATE (21)
        second byte is the sub_type

        sub_type 0 contains the lengths of things
        byte 2 is the number of expected packets after this.

        example: bytearray(b'\x15\x00\x18\x05\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x002')
        """
        sub_type = packet[1]
        if sub_type == 255:
            logger.info("error response from heart rate log request")
            self.reset()
            return NoData()
        if self.is_today() and sub_type == 23:
            return self._complete_log()
        if sub_type == 0:
            self.end = False
            self.size = packet[2]  # number of expected packets
            # Byte 3 is the slot grid in minutes. R02 firmware reports 5
            # (one slot per 5 minutes, samples sparser); the R08 reports its
            # sampling interval (5/10/15/20/30/60) with one slot per sample.
            # Treat anything implausible as the legacy 5-minute grid.
            reported_range = packet[3]
            if 1 <= reported_range <= self.MAX_RANGE_MINUTES:
                self.range = reported_range
            else:
                logger.warning(
                    f"Implausible heart-rate slot range {reported_range},"
                    " defaulting to 5 minutes"
                )
                self.range = 5
            self._raw_heart_rates = [-1] * (self.size * 13)
            return None
        if sub_type == 1:
            # next 4 bytes are a timestamp
            ts = struct.unpack_from("<l", packet, offset=2)[0]
            self.timestamp = datetime.fromtimestamp(ts, UTC)
            # TODO timezone?

            # remaining 16 - type - subtype - 4 - crc = 9
            self._raw_heart_rates[0:9] = list(packet[6:-1])
            self.index += 9
        else:
            self._raw_heart_rates[self.index : self.index + 13] = list(packet[2:15])
            self.index += 13
        # The last data packet closes the log. Note this can be sub_type 1:
        # a coarse-grid log (R08 at 60-min slots) can fit the whole day in
        # header + one data packet.
        if sub_type == self.size - 1:
            return self._complete_log()
        return None

    def _complete_log(self) -> HeartRateLog:
        assert self.timestamp
        result = HeartRateLog(
            heart_rates=self.heart_rates,
            timestamp=self.timestamp,
            size=self.size,
            range=self.range,
            index=self.index,
        )
        self.reset()
        return result

    @property
    def heart_rates(self) -> list[int]:
        """
        Normalize and clean heart rate logs

        Pad/trim the received slots to the day's slot count for the ring's
        declared grid (a 5-min ring has 288 slots, a 10-min ring 144, ...),
        and zero the slots that shouldn't exist yet because they are in the
        future. The raw buffer is ``size * 13`` bytes, which slightly
        overshoots the day on the 5-min grid (299 > 288) and undershoots
        nothing on coarser grids, so both directions are still possible.
        """
        slot_count = max(1, date_utils.MINUTES_PER_DAY // self.range)
        hr = self._raw_heart_rates.copy()

        if len(hr) > slot_count:
            hr = hr[0:slot_count]
        elif len(hr) < slot_count:
            hr.extend([0] * (slot_count - len(hr)))

        # TODO see if we can remove this
        # need a good reason why parsing should depend on the day
        # index might be good enough to indicate how much "valid" data we've gotten
        if self.is_today():
            m = date_utils.minutes_so_far(datetime.now(tz=UTC)) // self.range
            hr[m:] = [0] * len(hr[m:])

        return hr
