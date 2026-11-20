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

Swipe direction is asserted as fact: the mapping that started life as a guess
was settled by a labelled on-device test, recorded in the codec's
USAGE_GESTURES comment, so the replay below pins which usage decodes to which
gesture. The script also checks the codec's unverified-gestures machinery stays
self-consistent, since that is what callers warn from.
"""

from __future__ import annotations

import dataclasses
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

from custom_components.colmi_ring.colmi_codec import notifications, r08  # noqa: E402

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
    (bytes([0x01, 0x00, 0x00]), 7),  # Scan Next - swipe down
    (bytes([0x02, 0x00, 0x00]), 3),  # Scan Previous - swipe up
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
                r08.GESTURE_SWIPE_UP: 3,
                r08.GESTURE_SWIPE_DOWN: 7,
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

    scan_next = r08.parse_touch_report(bytes([0x01, 0x00, 0x00]))
    scan_previous = r08.parse_touch_report(bytes([0x02, 0x00, 0x00]))
    check(
        "the two swipe usages are distinct",
        scan_next is not None
        and scan_previous is not None
        and scan_next.gestures != scan_previous.gestures
        and not (scan_next.gestures & scan_previous.gestures),
    )
    # Which physical direction each usage means, now measured rather than
    # guessed (see USAGE_GESTURES). Reversing the codec's table fails here
    # rather than on someone's finger.
    check(
        "Scan Previous is swipe up, Scan Next is swipe down",
        scan_next is not None
        and scan_next.gestures == frozenset({r08.GESTURE_SWIPE_DOWN})
        and scan_previous is not None
        and scan_previous.gestures == frozenset({r08.GESTURE_SWIPE_UP}),
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
            and data["tap_count"] == 1
            and data["source"] == "touch",
            str(data),
        )

    # A raw report is one tap by definition; only TouchManager's coalescing
    # raises the count, via dataclasses.replace on the held report. Pin the
    # round trip the manager relies on so a codec refactor cannot silently
    # drop tap_count from the published event data.
    chained = dataclasses.replace(tap, tap_count=3)
    check("a fresh report decodes as one tap", tap is not None and tap.tap_count == 1)
    check(
        "a coalesced report keeps its gestures and reports the chain length",
        chained.gestures == tap.gestures
        and chained.usages == tap.usages
        and chained.raw == tap.raw
        and chained.tap_count == 3
        and chained.as_event_data()["tap_count"] == 3,
    )


def check_notice_parsing() -> None:
    """
    Unsolicited ``0x3B`` frames the ring could push by itself.

    Pinned against the QRing sub-command table: sleep and wake are the two
    states of the ``02 02`` frame, "off" is *only* the exact all-zero
    ``02 01 00 00`` shape (an ``02 01`` with an appType/strength tail is
    QRing's gesture-mode write and must not classify as a disable), and the
    config echo - the one frame an open session really does send - classifies
    as nothing.
    """
    print("touch notices (unsolicited 3B frames):")
    sleep = oem_frame(0x3B, [2, 2, 1, 0])
    wake = oem_frame(0x3B, [2, 2, 1, 1])
    off = oem_frame(0x3B, [2, 1, 0, 0])
    check("02 02 appType 00 is a sleep", r08.parse_touch_notice(sleep) == r08.TOUCH_NOTICE_SLEEP, hexed(sleep))
    check("02 02 appType 01 is a wake", r08.parse_touch_notice(wake) == r08.TOUCH_NOTICE_WAKE, hexed(wake))
    check(
        "02 02 appType 02 (unknown state) is no notice",
        r08.parse_touch_notice(oem_frame(0x3B, [2, 2, 1, 2])) is None,
    )
    check(
        "the touch-off shape is an off notice",
        r08.parse_touch_notice(off) == r08.TOUCH_NOTICE_OFF,
        hexed(off),
    )
    check(
        "a wake frame is not read as off",
        r08.parse_touch_notice(wake) != r08.TOUCH_NOTICE_OFF,
    )
    check(
        "02 01 with a tail is no notice (gesture write)",
        r08.parse_touch_notice(oem_frame(0x3B, [2, 1, 1, 2])) is None,
    )
    check(
        "the config echo is no notice",
        r08.parse_touch_notice(r08.touch_config_packet()) is None,
    )
    check(
        "a battery frame is no notice",
        r08.parse_touch_notice(bytearray(16)) is None,
    )
    wrong_opcode = bytearray(off)
    wrong_opcode[0] = 0x3C
    check("rejects wrong opcode", r08.parse_touch_notice(wrong_opcode) is None)
    check("rejects a short packet", r08.parse_touch_notice(bytearray([0x3B, 0x02, 0x01])) is None)

    # The exact frame from the field log that motivated this parser:
    # 3b 02 01 00 ... 3e, checksummed and all.
    logged = oem_frame(0x3B, [2, 1, 0, 0])
    logged_hex = "3B 02 01 00 00 00 00 00 00 00 00 00 00 00 00 3E"
    check("the logged field frame is an off notice", r08.parse_touch_notice(logged) == r08.TOUCH_NOTICE_OFF, logged_hex)
    check("the logged field frame carries a real checksum", logged[15] == 0x3E, f"{logged[15]:#04x}")


def check_notifications() -> None:
    """Pin the 0x73 notification channel: battery push, live activity, doorbells."""
    print("notifications (0x73):")
    # The frame captured from a user's HA log, byte for byte: 100 % charging.
    push = oem_frame(0x73, [0x0C, 0x64, 0x01])
    info = notifications.parse_battery_push(push)
    check(
        "field frame decodes",
        info is not None and info.battery_level == 100 and info.charging,
        show("100% charging", push),
    )
    off = notifications.parse_battery_push(oem_frame(0x73, [0x0C, 42, 0x00]))
    check(
        "charging flag off",
        off is not None and off.battery_level == 42 and not off.charging,
    )
    check(
        "wrong opcode is no battery",
        notifications.parse_battery_push(oem_frame(0x03, [0x0C, 64, 0x01])) is None,
    )
    check(
        "unknown sub-command is no battery",
        notifications.parse_battery_push(oem_frame(0x73, [0x0D, 64, 0x01])) is None,
    )
    check(
        "short frame is no battery",
        notifications.parse_battery_push(bytearray(b"\x73\x0c")) is None,
    )
    check(
        "all-zero frame is no battery",
        notifications.parse_battery_push(bytearray(16)) is None,
    )

    # Live activity (sub 0x12): three 24-bit little-endian day counters.
    # Synthesise a day-so-far frame: 10 000 steps, 450 000 raw calories
    # (450 kcal after the integration's /1000), 8 123 m.
    activity = oem_frame(
        0x73,
        [
            0x12,
            0x10 & 0xFF, (10_000 >> 8) & 0xFF, (10_000 >> 16) & 0xFF,
            450_000 & 0xFF, (450_000 >> 8) & 0xFF, (450_000 >> 16) & 0xFF,
            8_123 & 0xFF, (8_123 >> 8) & 0xFF, (8_123 >> 16) & 0xFF,
        ],
    )
    act = notifications.parse_live_activity(activity)
    check(
        "live activity decodes",
        act is not None
        and act.steps == 10_000
        and act.calories_raw == 450_000
        and act.distance_m == 8_123,
        show("10000 steps 450kcal 8123m", activity),
    )
    tiny = notifications.parse_live_activity(bytearray(b"\x73\x12\x01\x00\x00\x0a"))
    check(
        "short live activity frame is None",
        tiny is None,
        "fewer than bytes 2..10 present",
    )
    check(
        "wrong sub is no live activity",
        notifications.parse_live_activity(oem_frame(0x73, [0x13, 1, 2, 3, 4, 5, 6, 7, 8, 9])) is None,
    )
    check(
        "wrong opcode is no live activity",
        notifications.parse_live_activity(oem_frame(0x74, [0x12, 1, 2, 3, 4, 5, 6, 7, 8, 9])) is None,
    )
    check("all-zero frame is no live activity", notifications.parse_live_activity(bytearray(16)) is None)

    # Sub dispatch: every observed kind classifies, foreign opcodes do not.
    check(
        "subs classify",
        all(
            notifications.notification_sub(oem_frame(0x73, [sub, 0, 0])) == sub
            for sub in (0x01, 0x03, 0x04, 0x0C, 0x12)
        ),
    )
    check(
        "doorbells carry no values anyone could mistake",
        notifications.notification_sub(oem_frame(0x73, [0x01, 0, 0])) == notifications.SUB_NEW_HR
        and notifications.notification_sub(oem_frame(0x73, [0x03, 0, 0])) == notifications.SUB_NEW_SPO2
        and notifications.notification_sub(oem_frame(0x73, [0x04, 0, 0])) == notifications.SUB_NEW_STEPS,
    )
    check("foreign opcode is no notification", notifications.notification_sub(bytearray([0x03] + [0] * 15)) is None)
    check("notification channel constant", notifications.CMD_NOTIFICATION == 0x73)


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
    """The unverified-gestures machinery has to stay self-consistent."""
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
    check_notice_parsing()
    print()
    check_notifications()
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
