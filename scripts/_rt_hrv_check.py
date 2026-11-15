"""Throwaway check: R08 real-time codec changes (start sub-byte, 0x6A parse).

Run from the repo root:

    python scripts/_rt_hrv_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "colmi_ring"))

from colmi_codec import real_time as rt  # noqa: E402


def frame(prefix: list[int]) -> bytearray:
    """Whole-frame prefix (cmd byte first) padded to 16 bytes + checksum."""
    packet = bytearray(16)
    for i, b in enumerate(prefix):
        packet[i] = b
    packet[15] = sum(packet[:15]) & 0xFF
    return packet


def main() -> None:
    for name in ("HEART_RATE", "SPO2", "PRESSURE", "HRV"):
        packet = rt.get_start_packet(rt.RealTimeReading[name])
        print(f"{name:10s} start: {packet.hex(' ')}")
    print("HRV        stop :", rt.get_stop_packet(rt.RealTimeReading.HRV).hex(" "))

    # [6A, kind=10, value=47, error=0] -> Reading(47)
    ok = frame([106, 10, 47, 0])
    print("parse ok  :", rt.parse_stop_reading(ok))
    # [6A, kind=10, value=0, error=1] -> ReadingError(1) ("not worn")
    err = frame([106, 10, 0, 1])
    print("parse err :", rt.parse_stop_reading(err))
    # [6A, kind=0, value=0, error=0] -> Reading(0) ("no value")
    zero = frame([106, 10, 0, 0])
    print("parse zero:", rt.parse_stop_reading(zero))
    # Unknown kind -> None (ignored, must not raise in dispatch)
    weird = frame([106, 99, 5, 0])
    print("parse unk :", rt.parse_stop_reading(weird))

    assert rt.parse_stop_reading(ok) == rt.Reading(
        kind=rt.RealTimeReading.HRV, value=47
    )
    assert rt.parse_stop_reading(err) == rt.ReadingError(
        kind=rt.RealTimeReading.HRV, code=1
    )
    assert rt.parse_stop_reading(zero) == rt.Reading(
        kind=rt.RealTimeReading.HRV, value=0
    )
    assert rt.parse_stop_reading(weird) is None
    assert rt.get_start_packet(rt.RealTimeReading.HEART_RATE)[2] == rt.Action.START
    assert (
        rt.get_start_packet(rt.RealTimeReading.HRV)[2]
        == rt.VENDOR_START_SUBTYPE
        == 0x25
    )
    print("all assertions passed")


if __name__ == "__main__":
    main()
