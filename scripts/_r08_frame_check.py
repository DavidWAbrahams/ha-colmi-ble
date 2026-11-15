#!/usr/bin/env python3
"""Print the R08 touch frames the dev probe sends, with their checksums.

Run: python scripts/_r08_frame_check.py

Keeps the hex we paste into ``colmi_ring.debug_packet_probe`` honest: the
frames follow the Access Bridge recipe (16 bytes, opcode in byte 0, payload
1..14, byte 15 = sum(bytes 0..14) & 0xFF) and are built with the same
``make_packet`` the integration uses.
"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_make_packet():
    """
    Load ``make_packet`` straight from the codec file.

    Loaded by path rather than by package import so the check does not execute
    the integration's ``__init__`` (which imports Home Assistant).
    """
    path = _ROOT / "custom_components" / "colmi_ring" / "colmi_codec" / "packet.py"
    spec = importlib.util.spec_from_file_location("colmi_packet", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_packet


make_packet = _load_make_packet()


def show(label: str, opcode: int, payload: list[int]) -> None:
    """Print one frame as the hex the probe service expects."""
    packet = make_packet(opcode, bytearray(payload))
    expected = sum(packet[:15]) & 0xFF
    hexed = packet.hex(" ").upper()
    print(f"{label:22s} {hexed}")
    print(
        f"{'':22s} len={len(packet)} byte15={packet[15]:#04x} "
        f"sum0..14={expected:#04x} match={packet[15] == expected}"
    )


def main() -> int:
    # touchConfig(appType, sleepMinutes) -> 0x3B 0x02 0x00 appType sleep
    # appType 1 = documented media-key profile, 4 = touch fallback.
    show("touchConfig(1, 2)", 0x3B, [0x02, 0x00, 0x01, 0x02])
    show("touchConfig(4, 2)", 0x3B, [0x02, 0x00, 0x04, 0x02])
    show("touchConfig(1, 5)", 0x3B, [0x02, 0x00, 0x01, 0x05])
    show("touchConfig(4, 5)", 0x3B, [0x02, 0x00, 0x04, 0x05])
    # gestureOff() -> 0x3B 0x02 0x01 0x00 0x00
    show("gestureOff", 0x3B, [0x02, 0x01, 0x00, 0x00])
    # touchWake(appType) -> 0x3B 0x02 0x02 appType 0x01
    show("touchWake(1)", 0x3B, [0x02, 0x02, 0x01, 0x01])
    print()
    print("# appType 0..7 sweep, for if 1 and 4 both stay silent")
    for app_type in range(8):
        show(f"touchConfig({app_type}, 2)", 0x3B, [0x02, 0x00, app_type, 0x02])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
