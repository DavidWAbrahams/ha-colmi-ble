#!/usr/bin/env python3
"""
Self-test for the R09-family timed raw stream (0x69 type 13).

Covers the pieces that do not need a ring: the start/stop frames the driver
writes, the packet decode (layout, bounds, negative axes), and the
model-name gate that decides which stream generation a ring gets. The stream
behaviour that *does* need a ring - does ``69 06 04`` really stop it, what is
the accelerometer's LSB/g - belongs to the field probe, not this file.

Run: ``python scripts/_rt_stream_check.py`` (exit 0 = all checks pass).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))


def _bootstrap_package(name: str, path: Path) -> types.ModuleType:
    """
    Register *name* as a package without executing its ``__init__``.

    The real integration ``__init__.py`` imports ``homeassistant``, which a
    plain dev checkout does not have, while the codec and const are pure.
    """
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


if "custom_components.colmi_ring" not in sys.modules:  # pragma: no cover
    _bootstrap_package("custom_components", REPO_ROOT / "custom_components")
    _bootstrap_package(
        "custom_components.colmi_ring",
        REPO_ROOT / "custom_components" / "colmi_ring",
    )

from custom_components.colmi_ring.colmi_codec import motion  # noqa: E402
from custom_components.colmi_ring.const import (  # noqa: E402
    supports_realtime_motion_stream,
)

_FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record a pass/fail and print one line either way."""
    if ok:
        print(f"  ok   {name}")
    else:
        suffix = f" - {detail}" if detail else ""
        print(f"  FAIL {name}{suffix}")
        _FAILURES.append(name)


def _checksum(packet: bytearray) -> int:
    # Mirrors colmi_codec.packet.checksum: sum of the first 15 bytes (the
    # checksum byte itself is still 0 while the real one is being computed).
    return sum(packet[:15]) & 0xFF


def check_frames() -> None:
    """Start and stop frames match the field-verified wire shapes."""
    print("frames:")
    start = motion.get_realtime_stream_start_packet(120)
    # 69 0d 78 00 ...  (120 s little-endian), 16 bytes, valid checksum.
    check("start cmd/type", start[0] == 0x69 and start[1] == 0x0D, start.hex())
    check(
        "start duration LE 120",
        start[2] == 0x78 and start[3] == 0x00,
        start.hex(),
    )
    check("start length 16", len(start) == 16, str(len(start)))
    check("start checksum", start[-1] == _checksum(start), start.hex())

    big = motion.get_realtime_stream_start_packet(65535)
    check(
        "start duration LE 65535",
        big[2] == 0xFF and big[3] == 0xFF,
        big.hex(),
    )
    low = motion.get_realtime_stream_start_packet(1)
    check(
        "start clamps up to 10 s",
        low[2] == 0x0A and low[3] == 0x00,
        low.hex(),
    )
    high = motion.get_realtime_stream_start_packet(99999)
    check(
        "start clamps down to 65535 s",
        high[2] == 0xFF and high[3] == 0xFF,
        high.hex(),
    )

    stop = motion.get_realtime_stream_stop_packet()
    # 69 06 04 ... - start type 6, action stop; the verified stop.
    check("stop is 69 06 04", stop[0] == 0x69 and stop[1] == 0x06 and stop[2] == 0x04, stop.hex())  # noqa: E501
    check("stop length 16", len(stop) == 16, str(len(stop)))
    check("stop checksum", stop[-1] == _checksum(stop), stop.hex())


def _frame(
    *,
    ptype: int = 0x0D,
    error: int = 0,
    seq: int = 0,
    status: int = 0x61,
    opt_a: int = 1000,
    opt_b: int = 2000,
    ax: int = 0,
    ay: int = 0,
    az: int = 0,
) -> bytearray:
    """Build a 16-byte 0x69 type-13 packet from named fields."""
    pkt = bytearray(16)
    pkt[0] = 0x69
    pkt[1] = ptype
    pkt[2] = error
    pkt[3] = seq
    pkt[4] = status
    pkt[5] = opt_a & 0xFF
    pkt[6] = (opt_a >> 8) & 0xFF
    pkt[7] = opt_b & 0xFF
    pkt[8] = (opt_b >> 8) & 0xFF
    for offset, value in ((9, ax), (11, ay), (13, az)):
        raw = value & 0xFFFF
        pkt[offset] = raw & 0xFF
        pkt[offset + 1] = (raw >> 8) & 0xFF
    pkt[15] = _checksum(pkt)
    return pkt


def check_decode() -> None:
    """The type-13 sample decode: positives, negatives, bounds, rejection."""
    print("decode:")
    sample = motion.parse_realtime_accel_packet(
        _frame(ax=1000, ay=-1000, az=20000)
    )
    check("sample parsed", sample is not None)
    if sample is not None:
        # Raw units until REALTIME_ACCEL_LSB_PER_G is calibrated (== 1.0).
        check("x wire order", abs(sample.x - 1000.0) < 1e-9, str(sample.x))
        check("y negative", abs(sample.y + 1000.0) < 1e-9, str(sample.y))
        check("z large positive", abs(sample.z - 20000.0) < 1e-9, str(sample.z))

    edge = motion.parse_realtime_accel_packet(_frame(ax=-32768, ay=32767, az=0))
    check("s16 extremes", edge is not None and edge.x == -32768.0 and edge.y == 32767.0, "")  # noqa: E501

    # Wrong command byte (an HR reply on the same channel) is rejected.
    hr = bytearray(_frame(ax=5, ay=5, az=5))
    hr[1] = 0x01  # HR real-time type, not 13
    check("rejects HR packet", motion.parse_realtime_accel_packet(hr) is None)
    # Wrong type 14 (SpO2 pair) is not consumed as motion.
    check("rejects type 14", motion.parse_realtime_accel_packet(_frame(ptype=0x0E)) is None)  # noqa: E501
    # A truncated frame must not raise on the hot path.
    check("rejects short frame", motion.parse_realtime_accel_packet(bytearray([0x69, 0x0D, 0x00])) is None)  # noqa: E501


def check_gate() -> None:
    """The model-name gate: R09 and up stream via 0x69, older via 0xA1."""
    print("gate:")
    check("R09_ on", supports_realtime_motion_stream("R09_5703"))
    check("R10 on", supports_realtime_motion_stream("R10_ABCD"))
    check("R12 on", supports_realtime_motion_stream("R12_ABCD"))
    check("R2 on", not supports_realtime_motion_stream("R2_ABCD"))
    check("R08 off", not supports_realtime_motion_stream("R08_5703"))
    check("R8_ off", not supports_realtime_motion_stream("R8_5703"))
    check("R07 off", not supports_realtime_motion_stream("R07"))
    check("empty off", not supports_realtime_motion_stream(""))
    check("None off", not supports_realtime_motion_stream(None))
    check("garbage off", not supports_realtime_motion_stream("Colmi Ring"))
    # A bare, unprefixed number is not a model name: no leading 'R'.
    check("no prefix off", not supports_realtime_motion_stream("09"))


def main() -> int:
    check_frames()
    check_decode()
    check_gate()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s): {', '.join(_FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
