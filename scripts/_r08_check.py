#!/usr/bin/env python3
"""Offline selftest for the R08 touch codec: frames, and the Phase 0 capture.

Run: python scripts/_r08_check.py

Two things get pinned here.

1. **The enable frames.** Rebuilt through ``make_packet`` and compared against
   the byte sequences the R08 Access Bridge app sends (``3B 02 00 <appType>
   <min>``, ``3B 02 01 00 00``, ``3B 02 02 <appType> 01``) including the
   trailing checksum, so a refactor of the codec that changes a byte on the wire
   fails here rather than on a ring.

2. **The report decode.** Replays the packet tally from the Phase 0 probe run 3
   - 50 Consumer Control notifications on ``2a4d#68``, of which 15 were
   ``08 00 00`` (Play/Pause, the tester pressing the button), 7 were
   ``01 00 00`` (Scan Next), 3 were ``02 00 00`` (Scan Previous) and 25 were
   ``00 00 00`` (release) - and checks the decoder produces the same tally.
   The capture records counts, not the interleaving, so the replay is grouped
   by value; the counts are what the probe measured.

Swipe direction is deliberately not asserted as fact: the direction mapping is
flagged unverified in the codec, and this script asserts that the flag is
honest rather than asserting which way is up.
"""

from __future__ import annotations

import sys
import types
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))


def _bootstrap_package(name: str, path: Path) -> types.ModuleType:
    """
    Register *name* as a package without executing its ``__init__``.

    ``custom_components.colmi_ring/__init__.py`` imports Home Assistant, which a
    plain dev checkout does not have. The codec is pure, so pointing the import
    machinery at the directory lets it load on its own.
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

from custom_components.colmi_ring.colmi_codec import r08  # noqa: E402

_FAILURES: list[str] = []
_CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one assertion result."""
    global _CHECKS
    _CHECKS += 1
    if ok:
        print(f"[OK]   {name}")
    else:
        _FAILURES.append(name)
        print(f"[FAIL] {name}{f' - {detail}' if detail else ''}")


def hexed(packet: bytes | bytearray) -> str:
    """Upper-case space-separated hex, matching how frames are quoted in logs."""
    return " ".join(f"{b:02X}" for b in packet)


def show(label: str, packet: bytes | bytearray) -> str:
    """Print one frame and return its hex, for the frame section."""
    print(f"  {label:24s} {hexed(packet)}")
    return hexed(packet)


# ---------------------------------------------------------------------------
# 1. Enable frames
# ---------------------------------------------------------------------------


def oem_frame(opcode: int, payload: list[int]) -> bytearray:
    """Build a frame the Access Bridge recipe, independent of ``make_packet``."""
    packet = bytearray(16)
    packet[0] = opcode & 0xFF
    for offset, value in enumerate(payload, start=1):
        packet[offset] = value & 0xFF
    packet[15] = sum(packet[:15]) & 0xFF
    return packet


def check_frames() -> None:
    """Compare the codec's frames with the OEM app's, byte for byte."""
    print("enable frames (codec vs Access Bridge recipe):")
    cases: list[tuple[str, bytearray, bytearray]] = [
        (
            "touchConfig(1, 2)",
            r08.touch_config_packet(1, 2),
            oem_frame(0x3B, [2, 0, 1, 2]),
        ),
        ("touchConfig(1, 5)", r08.touch_config_packet(), oem_frame(0x3B, [2, 0, 1, 5])),
        (
            "touchConfig(4, 2)",
            r08.touch_config_packet(4, 2),
            oem_frame(0x3B, [2, 0, 4, 2]),
        ),
        ("touchOff", r08.touch_off_packet(), oem_frame(0x3B, [2, 1, 0, 0])),
        ("touchWake(1)", r08.touch_wake_packet(), oem_frame(0x3B, [2, 2, 1, 1])),
    ]
    for label, ours, theirs in cases:
        show(label, ours)
        check(
            f"{label} matches OEM bytes",
            bytes(ours) == bytes(theirs),
            f"codec={hexed(ours)} oem={hexed(theirs)}",
        )
        check(
            f"{label} checksum is sum(0..14)",
            ours[15] == sum(ours[:15]) & 0xFF,
            f"byte15={ours[15]:#04x}",
        )
        check(f"{label} is 16 bytes", len(ours) == 16, f"len={len(ours)}")

    # The header bytes are what the ring dispatches on; name them explicitly so
    # reordering the payload is caught even if the checksum happens to still fit.
    config = r08.touch_config_packet(1, 2)
    check(
        "touchConfig header is 3B 02 00 appType sleep",
        list(config[:5]) == [0x3B, 0x02, 0x00, 1, 2],
        hexed(config[:5]),
    )
    check(
        "touchConfig defaults are appType=1 sleep=5min",
        bytes(r08.touch_config_packet()) == bytes(r08.touch_config_packet(1, 5)),
    )
    off = r08.touch_off_packet()
    check(
        "touchOff header is 3B 02 01 00 00", list(off[:5]) == [0x3B, 0x02, 0x01, 0, 0]
    )
    wake = r08.touch_wake_packet(1)
    check(
        "touchWake header is 3B 02 02 appType 01",
        list(wake[:5]) == [0x3B, 0x02, 0x02, 1, 1],
    )

    # appType 0..7 all stay inside one byte: the probe sweep relies on it.
    sweep_ok = all(
        r08.touch_config_packet(app_type, 2)[3] == app_type for app_type in range(8)
    )
    check("appType 0..7 fits one byte", sweep_ok)


def check_reply_parsing() -> None:
    """The ring answers by echoing; acceptance has to survive that."""
    echo = r08.touch_config_packet(1, 2)
    check("accepts the config echo", r08.parse_touch_config_reply(echo))
    check("accepts the off echo", r08.parse_touch_config_reply(r08.touch_off_packet()))
    check(
        "accepts the wake echo", r08.parse_touch_config_reply(r08.touch_wake_packet(1))
    )
    check(
        "rejects a battery packet",
        not r08.parse_touch_config_reply(bytearray(16)),
        "all-zero frame read as a touch ack",
    )
    wrong_opcode = bytearray(r08.touch_off_packet())
    wrong_opcode[0] = 0x3C
    check("rejects wrong opcode", not r08.parse_touch_config_reply(wrong_opcode))
    wrong_sub = bytearray(r08.touch_off_packet())
    wrong_sub[2] = 0x03
    check("rejects unknown touch sub-type", not r08.parse_touch_config_reply(wrong_sub))
    check(
        "rejects a short packet",
        not r08.parse_touch_config_reply(bytearray([0x3B, 0x02])),
    )


# ---------------------------------------------------------------------------
# 2. The Phase 0 report capture
# ---------------------------------------------------------------------------

#: The Phase 0 probe run-3 tally, grouped by value: 50 notifications on
#: ``2a4d#68`` (the Consumer Control Input report), plus the one UART echo that
#: arrived on the same subscription and is checked separately above.
CAPTURE: tuple[tuple[bytes, int], ...] = (
    (bytes([0x08, 0x00, 0x00]), 15),  # Play/Pause - fifteen button presses
    (bytes([0x01, 0x00, 0x00]), 7),  # Scan Next - one swipe direction
    (bytes([0x02, 0x00, 0x00]), 3),  # Scan Previous - the other
    (bytes([0x00, 0x00, 0x00]), 25),  # release, one per press
)


def check_capture() -> None:
    """Replay the capture and compare tallies."""
    packets = [packet for packet, count in CAPTURE for _ in range(count)]
    events = [r08.parse_touch_report(packet) for packet in packets]

    decoded = [event for event in events if event is not None]
    releases = [event for event in events if event is None]

    check("capture replays 50 notifications", len(events) == 50, f"{len(events)}")
    check("all-zero reports decode to None", len(releases) == 25, f"{len(releases)}")
    check("non-zero reports decode", len(decoded) == 25, f"{len(decoded)}")

    usage_tally = Counter(usage for event in decoded for usage in event.usages)
    check(
        "usage tally matches the capture",
        usage_tally == Counter({"play_pause": 15, "scan_next": 7, "scan_previous": 3}),
        str(dict(usage_tally)),
    )

    gesture_tally = Counter(gesture for event in decoded for gesture in event.gestures)
    check(
        "gesture tally matches the capture",
        gesture_tally
        == Counter(
            {
                r08.GESTURE_TAP: 15,
                r08.GESTURE_SWIPE_UP: 7,
                r08.GESTURE_SWIPE_DOWN: 3,
            }
        ),
        str(dict(gesture_tally)),
    )

    # Every captured press maps to exactly one gesture: no combos, and none of
    # the three usages is left without an entity.
    check(
        "each captured press yields exactly one gesture",
        all(len(event.gestures) == 1 for event in decoded),
    )


def check_report_edge_cases() -> None:
    """Lengths, byte order, and a combo the firmware has not sent yet."""
    check(
        "rejects wrong lengths",
        all(
            r08.parse_touch_report(bytearray(size)) is None for size in (0, 1, 2, 4, 16)
        ),
    )
    check(
        "rejects a 16-byte Colmi frame",
        r08.parse_touch_report(r08.touch_config_packet()) is None,
        "a UART frame decoded as a touch report",
    )

    tap = r08.parse_touch_report(bytes([0x08, 0x00, 0x00]))
    check(
        "08 00 00 is Play/Pause",
        tap is not None and tap.usages == frozenset({"play_pause"}),
    )
    check(
        "Play/Pause is tap",
        tap is not None and tap.gestures == frozenset({r08.GESTURE_TAP}),
    )
    check("Play/Pause reports the raw bitmap", tap is not None and tap.raw == 0x0008)

    up = r08.parse_touch_report(bytes([0x01, 0x00, 0x00]))
    down = r08.parse_touch_report(bytes([0x02, 0x00, 0x00]))
    check(
        "the two swipe usages are distinct",
        up is not None
        and down is not None
        and up.gestures != down.gestures
        and not (up.gestures & down.gestures),
    )

    # Bit order: the Report Map declares fields 1 bit apart starting at the low
    # bit of byte 0, so bit 9 lives in byte 1 and bit 16 in byte 2.
    byte1 = r08.parse_touch_report(bytes([0x00, 0x02, 0x00]))
    byte2 = r08.parse_touch_report(bytes([0x00, 0x00, 0x01]))
    check("bit 9 decodes from byte 1", byte1 is not None and len(byte1.usages) == 1)
    check("bit 16 decodes from byte 2", byte2 is not None and len(byte2.usages) == 1)
    check(
        "unknown usages carry no gesture",
        byte1 is not None
        and not byte1.gestures
        and byte2 is not None
        and not byte2.gestures,
    )

    combo = r08.parse_touch_report(bytes([0x09, 0x00, 0x00]))
    check(
        "a two-bit report yields both usages and both gestures",
        combo is not None
        and combo.usages == frozenset({"play_pause", "scan_next"})
        and len(combo.gestures) == 2,
    )
    if combo is not None:
        data = combo.as_event_data()
        check(
            "event data is flat and deterministic",
            data["gesture"] == min(combo.gestures)
            and data["gestures"] == sorted(combo.gestures)
            and data["usages"] == sorted(combo.usages)
            and data["raw"] == "0x000009"
            and data["source"] == "touch",
            str(data),
        )


def check_usage_table() -> None:
    """Sanity-check the table the decoder indexes by bit position."""
    bits = [bit for bit, _ in r08.TOUCH_USAGE_BITS]
    names = [name for _, name in r08.TOUCH_USAGE_BITS]
    check("usage bit indices are unique", len(set(bits)) == len(bits))
    check(
        "usage names are unique",
        len(set(names)) == len(names),
        str([name for name, count in Counter(names).items() if count > 1]),
    )
    check(
        "usage bits cover the declared 18 fields", max(bits) >= 17, f"max={max(bits)}"
    )
    check(
        "every gesture maps from a real usage",
        all(usage in names for usage in r08.USAGE_GESTURES),
        str(sorted(set(r08.USAGE_GESTURES) - set(names))),
    )
    check(
        "tap maps from Play/Pause",
        r08.USAGE_GESTURES.get("play_pause") == r08.GESTURE_TAP,
    )


def check_unverified_flag() -> None:
    """The swipe-direction TODO has to stay honest."""
    swipes = {r08.GESTURE_SWIPE_UP, r08.GESTURE_SWIPE_DOWN}
    check(
        "unverified set matches the flag",
        (r08.UNVERIFIED_GESTURES == swipes) == bool(r08.SWIPE_DIRECTION_UNVERIFIED),
        f"flag={r08.SWIPE_DIRECTION_UNVERIFIED} set={sorted(r08.UNVERIFIED_GESTURES)}",
    )
    check(
        "only swipes are unverified",
        not (r08.UNVERIFIED_GESTURES & {r08.GESTURE_TAP}),
        "tap should be verified",
    )
    if r08.SWIPE_DIRECTION_UNVERIFIED:
        print("       note: swipe direction is unverified (plan Phase 0.5)")


def main() -> int:
    check_frames()
    print()
    check_reply_parsing()
    print()
    check_capture()
    print()
    check_report_edge_cases()
    print()
    check_usage_table()
    print()
    check_unverified_flag()
    print()
    if _FAILURES:
        print(f"FAILED {_FAILURES}/{_CHECKS} checks")
        return 1
    print(f"all {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
