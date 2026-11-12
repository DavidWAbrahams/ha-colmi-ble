#!/usr/bin/env python3
"""
_stress_selftest.py

Throwaway validation for colmi_codec.stress: feeds golden 0x37 bursts
through StressLogParser and checks slot mapping, termination and empty
answers. Run from the repo root:

    python scripts/_stress_selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "colmi_ring"))

from colmi_codec import stress  # noqa: E402
from colmi_codec.packet import checksum  # noqa: E402


def pkt(payload: list[int]) -> bytearray:
    """Build a 16-byte packet: 55, then payload bytes, pad, crc."""
    p = bytearray(16)
    p[0] = stress.CMD_READ_STRESS
    for i, b in enumerate(payload):
        p[1 + i] = b
    p[15] = checksum(p)
    return p


def check_request() -> None:
    p = stress.read_stress_packet()
    assert len(p) == 16, "request must be 16 bytes"
    assert p[0] == 55, f"cmd byte {p[0]}"
    assert p[1] == 0, "request has no payload"
    # checksum() sums the whole packet, CRC byte included (0 when computed).
    assert p[15] == sum(p[0:15]) & 255
    print("ok  request packet")


def check_full_burst() -> None:
    parser = stress.StressLogParser()

    # Header: 51 points, 30-minute slots.
    assert parser.parse(pkt([0x00, 51, 30])) is None

    # Packet 1: byte 2 = start offset (0), 12 values in bytes 3..14.
    vals1 = list(range(1, 13))  # slots 0..11 -> 1..12
    assert parser.parse(pkt([0x01, 0x00] + vals1)) is None

    # Packets 2..4: 13 values each in bytes 2..14.
    vals2 = list(range(20, 33))  # slots 12..24
    vals3 = [0] * 13  # slots 25..37 all zero (no samples)
    vals4 = list(range(40, 53))  # slots 38..50, trimmed to slot 47
    assert parser.parse(pkt([0x02] + vals2)) is None
    assert parser.parse(pkt([0x03] + vals3)) is None
    log = parser.parse(pkt([0x04] + vals4))
    assert isinstance(log, stress.StressLog), f"expected StressLog, got {log!r}"

    assert len(log.values) == 48, f"48 slots, got {len(log.values)}"
    assert log.values[0:12] == vals1, "packet 1 -> slots 0..11"
    assert log.values[12:25] == vals2, "packet 2 -> slots 12..24"
    assert log.values[25:38] == [0] * 13, "packet 3 -> zero slots kept"
    assert log.values[38:48] == vals4[:10], "packet 4 -> slots 38..47 (trimmed)"

    samples = log.samples_with_times()
    assert samples[0] == (1, log.timestamp), "first sample at midnight"
    assert samples[-1] == (49, log.timestamp.replace(hour=23, minute=30)), (
        f"last slot 47 -> 23:30, got {samples[-1]}"
    )
    assert len(samples) == 12 + 13 + 10, f"non-zero slots only: {len(samples)}"
    # A sample time must land on a 30-minute boundary.
    mid = samples[12]
    assert mid[1].hour == 6 and mid[1].minute == 0 and mid[0] == 20, (
        f"slot 12 -> 06:00, got {mid}"
    )
    print("ok  full burst slot mapping")


def check_empty() -> None:
    parser = stress.StressLogParser()
    res = parser.parse(pkt([0xFF]))
    assert isinstance(res, stress.NoData), f"expected NoData, got {res!r}"
    print("ok  empty answer -> NoData")


def check_state_resets() -> None:
    parser = stress.StressLogParser()
    parser.parse(pkt([0x00, 51, 30]))
    parser.parse(pkt([0x01, 0x00] + [7] * 12))
    # Ring answers "empty" mid-burst: parser must recover.
    assert isinstance(parser.parse(pkt([0xFF])), stress.NoData)
    # Then a clean burst right after must not carry old values.
    parser.parse(pkt([0x00, 51, 30]))
    parser.parse(pkt([0x01, 0x00] + [0] * 12))
    parser.parse(pkt([0x02] + [0] * 13))
    parser.parse(pkt([0x03] + [0] * 13))
    log = parser.parse(pkt([0x04] + [50] + [0] * 12))
    assert isinstance(log, stress.StressLog)
    # Packet 4 lands at slots 38..50; nothing from the aborted burst survives.
    assert log.values[0] == 0 and log.values[38] == 50, (
        f"clean re-parse: {log.values[38]}"
    )
    assert all(v == 0 for i, v in enumerate(log.values) if i != 38)
    print("ok  parser state resets after completion/NoData")


def main() -> int:
    check_request()
    check_full_burst()
    check_empty()
    check_state_resets()
    print("all stress codec checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
