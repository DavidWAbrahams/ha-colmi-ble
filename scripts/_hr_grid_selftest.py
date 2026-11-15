#!/usr/bin/env python3
"""
_hr_grid_selftest.py

Throwaway validation for colmi_codec.hr: the daily heart-rate log header
(byte 3 of the 0x15 sub_type 0 packet) declares the slot GRID in minutes,
which varies by firmware. R02-class firmware reports 5 (fixed 5-min grid,
samples sparser); the R08 reports its *sampling interval* as the grid
(e.g. 10 for a 10-min ring), one slot per sample. Stamping a coarse-grid
log on a 5-min grid puts every sample hours in the past, which the
coordinator's staleness check then blanks as "unknown" - the R08 HR bug.

Golden vectors come from the R08-Access-Bridge HeartAssembler tests
(60-min grid) and the upstream R02 example (5-min grid). Run from the
repo root:

    python scripts/_hr_grid_selftest.py
"""

from __future__ import annotations

import struct
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "colmi_ring"))

from colmi_codec import hr  # noqa: E402
from colmi_codec.packet import checksum  # noqa: E402


def pkt(sub_type: int, body: list[int]) -> bytearray:
    """Build a 16-byte 0x15 packet: cmd, sub_type, body..., pad, crc."""
    p = bytearray(16)
    p[0] = hr.CMD_READ_HEART_RATE
    p[1] = sub_type
    for i, b in enumerate(body):
        p[2 + i] = b
    p[15] = checksum(p)
    return p


def header(size: int, grid: int) -> bytearray:
    """sub_type 0 header: byte 2 = total packet count, byte 3 = grid minutes."""
    return pkt(0x00, [size, grid])


def data_packet(sub: int, values: list[int]) -> bytearray:
    """Data packets after the first carry 13 values in bytes 2..14."""
    assert len(values) == 13
    return pkt(sub, values)


def first_data_packet(ts: datetime, values: list[int]) -> bytearray:
    """First data packet: 4-byte LE timestamp in bytes 2..5, 9 values in 6..14."""
    assert len(values) == 9
    body = list(struct.pack("<l", int(ts.timestamp()))) + values
    return pkt(0x01, body)


def _yesterday_midnight() -> datetime:
    today = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1)


def check_r08_10min_grid() -> None:
    """An R08 10-min log must stamp slots 10 minutes apart, not 5."""
    parser = hr.HeartRateLogParser()
    midnight = _yesterday_midnight()

    size = 12  # header + 11 data packets; 9 + 10*13 = 139 slots of 144
    assert parser.parse(header(size, grid=10)) is None
    assert parser.range == 10

    values = [0] * 9
    values[3] = 62  # slot 3 -> 00:30
    assert parser.parse(first_data_packet(midnight, values)) is None

    log = None
    for sub in range(2, size):
        vals = [0] * 13
        if sub == 6:
            # slot = 9 + (6-2)*13 + 0 = 61 -> 10:10
            vals[0] = 71
        if sub == 7:
            # slot = 9 + 65 = 74 -> 12:20 (last sample of the day so far)
            vals[0] = 68
        log = parser.parse(data_packet(sub, vals))
        if log is not None:
            assert sub == size - 1, "parser terminated early"
            break
    assert isinstance(log, hr.HeartRateLog), f"expected HeartRateLog, got {log}"
    assert log.range == 10, f"grid lost: {log.range}"

    timed = {ts: bpm for bpm, ts in log.heart_rates_with_times() if bpm > 0}
    assert timed == {
        midnight + timedelta(minutes=30): 62,
        midnight + timedelta(minutes=610): 71,  # 10:10
        midnight + timedelta(minutes=740): 68,  # 12:20
    }, f"slot timestamps wrong: {sorted(timed.items())}"
    print("ok  R08 10-min grid stamps samples on a 10-min grid")


def check_bridge_60min_golden() -> None:
    """R08-Access-Bridge golden vectors: 2-packet 60-min-grid log."""
    parser = hr.HeartRateLogParser()
    # Their header: 15 00 02 3C ... (2 packets, 60-minute grid)
    assert parser.parse(bytearray.fromhex("1500023C000000000000000000000053")) is None
    assert parser.range == 60
    # Their data packet: timestamp + values ... 46 47 (0x46=70, 0x47=71).
    # sub_type 1 is the last of the 2 packets, so it must terminate.
    result = parser.parse(bytearray.fromhex("1501802A546A46470000000000000000 0B"))
    assert isinstance(result, hr.HeartRateLog), f"expected HeartRateLog, got {result}"
    timed = [(bpm, ts) for bpm, ts in result.heart_rates_with_times() if bpm > 0]
    assert [bpm for bpm, _ in timed] == [70, 71], f"values wrong: {timed}"
    t0, t1 = (ts for _, ts in timed)
    assert t1 - t0 == timedelta(minutes=60), f"60-min grid lost: {t1 - t0}"
    print("ok  R08-Access-Bridge 60-min golden vector parses with 60-min steps")


def check_r02_5min_golden() -> None:
    """The legacy R02 header (grid byte 5) keeps the historical 288/5-min grid."""
    # Upstream example header: b'\x15\x00\x18\x05...2'
    parser = hr.HeartRateLogParser()
    assert (
        parser.parse(
            bytearray(b"\x15\x00\x18\x05\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x002")
        )
        is None
    )
    assert parser.range == 5

    midnight = _yesterday_midnight()
    size = 0x18
    vals = [0] * 9
    vals[0] = 65  # slot 0 -> 00:00
    assert parser.parse(first_data_packet(midnight, vals)) is None
    log = None
    for sub in range(2, size):
        vals = [0] * 13
        if sub == 3:
            vals[10] = 77  # slot = 9 + 13 + 10 = 32 -> 02:40
        log = parser.parse(data_packet(sub, vals))
        if log is not None:
            break
    assert isinstance(log, hr.HeartRateLog), f"expected HeartRateLog, got {log}"
    assert log.range == 5
    assert len(log.heart_rates) == 288, "R02 day must stay 288 slots"
    timed = {ts: bpm for bpm, ts in log.heart_rates_with_times() if bpm > 0}
    assert timed[midnight] == 65
    assert timed[midnight + timedelta(minutes=160)] == 77
    print("ok  R02 5-min golden header still yields the 288-slot 5-min grid")


def check_implausible_range_falls_back() -> None:
    parser = hr.HeartRateLogParser()
    assert parser.parse(header(size=24, grid=0)) is None
    assert parser.range == 5, "grid 0 must fall back to the legacy 5-min grid"
    print("ok  implausible grid byte falls back to 5 minutes")


if __name__ == "__main__":
    check_r08_10min_grid()
    check_bridge_60min_golden()
    check_r02_5min_golden()
    check_implausible_range_falls_back()
    print("all hr grid selftests passed")
