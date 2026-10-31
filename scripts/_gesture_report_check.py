#!/usr/bin/env python3
"""Offline check of ``_accel_probe.report_camera_gesture``.

Running the probe needs a real ring, but deciding what a capture *means* is pure
logic over a packet list. This feeds the reporter synthetic captures for each
firmware behaviour it has to tell apart - most importantly "the firmware has no
camera-button gesture" versus "the gesture works, but RAW mode silences it",
which is the difference between giving up on claps and turning the Motion Stream
switch off to get them.

    python scripts/_gesture_report_check.py
"""

from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]


def _load_probe() -> Any:
    """Import the probe by path so its ``bleak`` import is the only dependency.

    Home Assistant is not needed, and the module is imported rather than copied
    so this check keeps testing the code the probe actually runs.
    """
    path = _ROOT / "scripts" / "_accel_probe.py"
    spec = importlib.util.spec_from_file_location("_accel_probe", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

GESTURE = bytes([probe.CMD_CAMERA_GESTURE, 0x04, 0x06])


def _capture(
    alone: int,
    with_raw: int | None,
    *,
    alone_disabled: int = 0,
    with_raw_disabled: int = 0,
) -> Any:
    """A capture whose gesture windows hold the given numbers of ``0x02`` packets.

    ``with_raw=None`` means the run never got as far as arming the gesture the
    second time, which is how an interrupted session looks. Marks are written on a
    made-up clock instead of ``Capture.note()`` so the windows have real durations
    and the packets provably fall inside them.
    """
    capture = probe.Capture()
    clock = 1000.0

    def phase(label: str, seconds: float, gestures: int | None) -> None:
        nonlocal clock
        capture.marks.append((clock, label))
        for index in range(gestures or 0):
            capture.packets.append(
                probe.Packet(
                    t=clock + 0.5 + index * 0.1, data=GESTURE, char="6e400003"
                )
            )
        clock += seconds

    phase("passive", 5, 0)
    phase("gesture_alone_on", 12, alone)
    phase("gesture_alone_off", 6, alone_disabled)
    phase("raw_stream", 20, 0)
    if with_raw is not None:
        phase("gesture_with_raw_on", 12, with_raw)
        phase("gesture_with_raw_off", 6, with_raw_disabled)
    return capture


def _no_gesture_phase() -> Any:
    """A capture from a run that died before the gesture was ever armed."""
    capture = probe.Capture()
    capture.marks.append((1000.0, "passive"))
    capture.marks.append((1005.0, "raw_stream"))
    return capture


def _text(capture: Any) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.report_camera_gesture(capture)
    return buf.getvalue()


def run(name: str, capture: Any, expect: str) -> bool:
    text = _text(capture)
    ok = expect in text
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(text)
    return ok


def main() -> int:
    checks = [
        run(
            "answers both ways -> camera mode confirmed and coexists with RAW",
            _capture(3, 4),
            "coexists with RAW mode",
        ),
        run(
            "answers only with RAW off -> confirmed but mutually exclusive",
            _capture(3, 0),
            "mutually exclusive",
        ),
        run(
            "never answers -> firmware has no gesture either way",
            _capture(0, 0),
            "no camera-button gesture at all",
        ),
        run(
            "answers only under RAW -> flagged as the wrong way round",
            _capture(0, 2),
            "wrong way round",
        ),
        run(
            "interrupted before the second arm -> inconclusive, not 'no gesture'",
            _capture(0, None),
            "inconclusive",
        ),
        run(
            "never reached the gesture at all -> no verdict claimed",
            _no_gesture_phase(),
            "(gesture phases not reached)",
        ),
        run(
            "counts are printed per phase, enabled and disabled",
            _capture(2, 1, alone_disabled=3, with_raw_disabled=4),
            "0x02 packets: 2 while enabled, 3 while disabled",
        ),
        run(
            "second phase counts print too",
            _capture(2, 1, alone_disabled=3, with_raw_disabled=4),
            "0x02 packets: 1 while enabled, 4 while disabled",
        ),
    ]
    passed = sum(checks)
    print(f"\n{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
