#!/usr/bin/env python3
"""Check our motion codec against the known-good reference web app.

The reference is ``mrfloydst/smartringmidi`` ``bluetooth.html`` (a Web MIDI demo
that turns a Colmi R02 into a MIDI controller), whose axis bit-shifting is
itself lifted from ``atc1441/ATC_RF03_Ring``. It is the only working
implementation of this protocol anyone has published, so it is the yardstick for
the parts of :mod:`custom_components.colmi_ring.colmi_codec.motion` that were
written from it as a best guess.

Unlike the rest of the Phase 0 checklist this needs **no ring**: the demo's
JavaScript is transcribed literally below (``js_*`` functions) and compared with
our Python, byte for byte and float for float, including a fuzz pass over the
whole 12-bit space. What it proves is parity with the reference; what it cannot
prove is that the reference matches *our* firmware. That part is what
``scripts/_accel_probe.py`` is for - in particular whether the R02 serves
accelerometer data on ``0xA1``/``packet[1] == 3`` at all, which the demo implies
but never demonstrates on an R02.

    python scripts/_motion_ref_parity.py          # exits non-zero on mismatch
    python scripts/_motion_ref_parity.py -v       # also print the reference's
                                                  # own MIDI angle output

Coverage:
* the four command frames (RAW on/off, camera gesture on/off), including the
  checksum byte, against the demo's ``arrayOut[15]`` accumulation;
* the axis decode - nibble packing, Y/Z/X wire order, 12-bit sign extension,
  the +/-4 g scale - over edge cases and 200k random packets;
* the two guards the demo has (``value[1] == 3``, enough bytes to read);
* the demo's angle maths, printed for reference since our sensors deliberately
  publish raw g instead of those angles.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))


def _bootstrap_package(name: str, path: Path) -> types.ModuleType:
    """Register *name* as a package without executing its ``__init__``.

    Same trick as :mod:`scripts._motion_replay`: the real integration
    ``__init__.py`` imports ``homeassistant``, which a plain dev checkout does
    not have, while the codec itself is pure.
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
from custom_components.colmi_ring.colmi_codec.packet import (  # noqa: E402
    make_packet,
)

if TYPE_CHECKING:  # pragma: no cover
    from custom_components.colmi_ring.colmi_codec.motion import AccelSample


# --------------------------------------------------------------------------
# The reference implementation, transcribed literally from bluetooth.html
# --------------------------------------------------------------------------


def js_int12(uint12: int) -> int:
    """``function int12(uint12)``."""
    return uint12 - 4096 if uint12 > 2047 else uint12


def js_convert_raw_to_g(raw_value: int) -> float:
    """``function convertRawToG(rawValue)`` - the demo's own +/-4 G comment."""
    range_g = 4
    return (raw_value / 2048) * range_g


def js_checksum(packet: list[int]) -> int:
    """The demo's ``arrayOut[15] = (arrayOut[15] + arrayOut[i]) & 0xff`` loop."""
    acc = 0
    for i in range(15):
        acc = (acc + packet[i]) & 0xFF
    return acc


def check_framing(rep: Reporter, rng: random.Random) -> None:
    """Our 16-byte framing must equal the demo's, for frames beyond the four."""
    print("== framing / checksum ==")
    mismatches = 0
    for _ in range(2000):
        command = rng.randint(0, 255)
        sub = bytearray(rng.randint(0, 255) for _ in range(rng.randint(0, 5)))
        ours = make_packet(command, sub) if sub else make_packet(command)
        # Built from the command and sub-data alone, the way the demo builds it:
        # a zeroed 16-byte frame, the bytes written from index 0, then the sum.
        theirs = [0] * 16
        for i, byte in enumerate([command, *sub]):
            theirs[i] = byte & 0xFF
        theirs[15] = js_checksum(theirs)
        if bytes(ours) != bytes(theirs):
            mismatches += 1
            print(f"     mismatch: cmd={command:02X} sub={sub.hex()}")
            break
    rep.check(
        "make_packet == the demo's frame builder (2000 random commands)",
        mismatches == 0,
    )


def js_command(hex_string: str) -> bytes:
    """What the demo writes for ``<button data-v="A10404">``, as 16 bytes."""
    data = [
        int(hex_string[i : i + 2], 16) for i in range(0, len(hex_string), 2)
    ]
    out = [0] * 16
    for i, byte in enumerate(data):
        out[i] = byte & 0xFF
    out[15] = js_checksum(out)
    return bytes(out)


def js_decode(value: list[int]) -> tuple[float, float, float] | None:
    """The notification handler's body: ``(Ax, Ay, Az)`` in g, or ``None``."""
    if value[1] != 3:
        return None
    raw_y = js_int12((((value[2] << 4) | (value[3] & 0xF)) & 0xFFF))
    raw_z = js_int12((((value[4] << 4) | (value[5] & 0xF)) & 0xFFF))
    raw_x = js_int12((((value[6] << 4) | (value[7] & 0xF)) & 0xFFF))
    return (
        js_convert_raw_to_g(raw_x),
        js_convert_raw_to_g(raw_y),
        js_convert_raw_to_g(raw_z),
    )


def js_rotate_cc(sample: AccelSample) -> tuple[int, int, int]:
    """``rotateX/Y/Z``: the demo's MIDI CC values, i.e. *angles* not raw axes."""
    x_val, y_val, z_val = sample.x, sample.y, sample.z
    ratio = 127 / math.pi
    return (
        int(
            (math.atan2(x_val, math.sqrt(y_val * y_val + z_val * z_val)) + math.pi / 2)
            * ratio,
        ),
        int(
            (math.atan2(y_val, math.sqrt(x_val * x_val + z_val * z_val)) + math.pi / 2)
            * ratio,
        ),
        int(
            (math.atan2(z_val, math.sqrt(x_val * x_val + y_val * y_val)) + math.pi / 2)
            * ratio,
        ),
    )


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class Reporter:
    """Tiny pass/fail printer with an exit code."""

    def __init__(self, verbose: bool = False) -> None:
        self.failures = 0
        self.verbose = verbose

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if not ok:
            self.failures += 1
        print(f"{'OK  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if detail else ""))
        return ok

    def note(self, text: str) -> None:
        if self.verbose:
            print(f"     {text}")


def position_packet(raw_y: int, raw_z: int, raw_x: int) -> bytearray:
    """Build the wire frame the reference expects from 12-bit raw axes."""
    packet = bytearray(16)
    packet[0] = motion.CMD_RAW_MODE
    packet[1] = motion.TYPE_POSITION
    for high_index, raw in ((2, raw_y), (4, raw_z), (6, raw_x)):
        value = raw & 0xFFF
        packet[high_index] = (value >> 4) & 0xFF
        packet[high_index + 1] = value & 0xF
    return packet


def check_commands(rep: Reporter) -> None:
    print("== command frames ==")
    cases = (
        ("RAW data on  (A10404)", motion.get_raw_start_packet(), "A10404"),
        ("RAW data off (A102)", motion.get_raw_stop_packet(), "A102"),
        ("camera on    (0204)", motion.get_camera_gesture_on_packet(), "0204"),
        ("camera off   (0206)", motion.get_camera_gesture_off_packet(), "0206"),
    )
    for label, ours, hex_command in cases:
        theirs = js_command(hex_command)
        rep.check(
            label,
            bytes(ours) == theirs,
            f"ours={ours[:3].hex()} cs={ours[15]:02X} ref={theirs[:3].hex()} cs={theirs[15]:02X}",
        )


def check_decode(rep: Reporter, fuzz: int) -> None:
    print("\n== axis decode (nibbles, Y/Z/X order, sign, +/-4 g scale) ==")
    edges = (
        (0, 0, 0),
        (2047, 2047, 2047),
        (-2048, -2048, -2048),
        (0, -1, 1),
        (1024, -1024, 512),
        (-2048, 2047, -1),
        (512, 2047, -2048),
    )
    for raw_y, raw_z, raw_x in edges:
        packet = position_packet(raw_y, raw_z, raw_x)
        theirs = js_decode(list(packet))
        ours = motion.parse_accel_packet(packet)
        assert theirs is not None and ours is not None
        rep.check(
            f"raw Y/Z/X = ({raw_y}, {raw_z}, {raw_x})",
            (ours.x, ours.y, ours.z) == theirs,
            f"ours=({ours.x:+.6f}, {ours.y:+.6f}, {ours.z:+.6f}) ref={theirs}",
        )

    rng = random.Random(20261029)
    mismatches = 0
    for _ in range(fuzz):
        packet = position_packet(
            rng.randint(-2048, 2047),
            rng.randint(-2048, 2047),
            rng.randint(-2048, 2047),
        )
        theirs = js_decode(list(packet))
        ours = motion.parse_accel_packet(packet)
        if ours is None or ours.x != theirs[0] or ours.y != theirs[1] or ours.z != theirs[2]:
            mismatches += 1
            print(f"     mismatch on {packet[:8].hex()}: ours={ours} ref={theirs}")
            break
    rep.check(f"fuzz over {fuzz} random packets", mismatches == 0)


def check_guards(rep: Reporter) -> None:
    print("\n== packet guards ==")
    short = position_packet(0, 0, 0)[:7]
    rep.check(
        "short packet -> None (reference would read undefined)",
        motion.parse_accel_packet(short) is None,
    )
    other_type = position_packet(0, 0, 0)
    other_type[1] = 2
    rep.check(
        "packet[1] != 3 -> None (reference skips via the same gate)",
        motion.parse_accel_packet(other_type) is None,
    )


def show_reference_angles(rep: Reporter, rest_up_axis: tuple[int, int, int]) -> None:
    """Print what the demo *displays*, to make the difference from raw g concrete."""
    print("\n== what the reference shows as \"Rotational data\" ==")
    print("     (angles scaled to 0..127 MIDI CC, NOT raw acceleration)")
    print(f"     {'raw Y/Z/X':>22} | {'g (x, y, z)':>26} | |g| | CC (X, Y, Z)")
    for axes in (rest_up_axis, (0, 1024, 0), (-2048, 0, 0), (1450, 1450, 0)):
        raw_y, raw_z, raw_x = axes
        sample = motion.parse_accel_packet(position_packet(raw_y, raw_z, raw_x))
        assert sample is not None
        print(
            f"     {str(axes):>22} | "
            f"({sample.x:+.3f}, {sample.y:+.3f}, {sample.z:+.3f})"
            f" | {sample.magnitude_g:4.2f} | {js_rotate_cc(sample)}",
        )
    rep.note(
        "A hand still and held upright should read ~1.00 on the sensor whose axis "
        "points up; the reference's equivalent is a CC of 63 or 127, which is where "
        "its atan2 + pi/2 offset puts level and vertical.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also print explanatory notes",
    )
    parser.add_argument(
        "--fuzz",
        type=int,
        default=200_000,
        help="random packets to compare (default 200000)",
    )
    parser.add_argument(
        "--rest-axis",
        choices=("x", "y", "z"),
        default="y",
        help="which axis points up for the worked example (default y)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rep = Reporter(verbose=args.verbose)

    check_commands(rep)
    check_framing(rep, random.Random(20261029))
    check_decode(rep, fuzz=args.fuzz)
    check_guards(rep)
    rest_up = {"x": (0, 0, 512), "y": (512, 0, 0), "z": (0, 512, 0)}[args.rest_axis]
    show_reference_angles(rep, rest_up)

    print()
    if rep.failures:
        print(f"{rep.failures} mismatch(es) against the reference - do not ship this decode.")
        return 1
    print("Full parity with the reference app for everything it defines.")
    print(
        "Still unproven without the ring: that our R02 answers these commands, and\n"
        "that it serves accel data on 0xA1 with packet[1] == 3. See _accel_probe.py.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
