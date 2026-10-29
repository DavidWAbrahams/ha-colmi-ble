#!/usr/bin/env python3
"""Synthesise a labelled accelerometer stream and replay it through the detector.

A hardware-free check that the gesture thresholds do something sensible: it
writes a CSV of a resting wrist, a clap, a fast hand raise and a fast hand
lower, then (unless ``--out`` is given) feeds it straight to
:mod:`scripts/_motion_replay.py`'s machinery and prints what fires.

    python scripts/_motion_synth.py                 # generate + report
    python scripts/_motion_synth.py --out data/synth.csv
    python scripts/_motion_synth.py --sensitivity 0.65

The signal is built in the ring's own frame, and the detector is gravity
referenced rather than axis referenced, so the axis choice below is arbitrary.
What it does model is the physics the detector relies on: an accelerometer reads
1 g *upward* at rest, and extra acceleration along that same "up" is what a
raising hand looks like.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(SCRIPTS_DIR))

if TYPE_CHECKING:
    from _motion_replay import Row

RATE_HZ = 25.0


def _push(
    rows: list[tuple[float, float, float, float]],
    t: float,
    up: tuple[float, float, float],
    spin: float,
    linear_up: float,
) -> float:
    """Emit one sample whose gravity points along ``up`` plus ``linear_up`` g."""
    x = up[0] + up[0] * linear_up
    y = up[1] + up[1] * linear_up
    z = up[2] + up[2] * linear_up
    # A little correlated noise: real sensors are never perfectly still.
    jitter = 0.02 * math.sin(t * 47.0) * spin
    rows.append((t, x + jitter, y + jitter, z + jitter))
    return t + 1.0 / RATE_HZ


def _slerp(
    a: tuple[float, float, float], b: tuple[float, float, float], u: float
) -> tuple[float, float, float]:
    """Unit-lerp between two directions, normalised (u in 0..1)."""
    return (
        a[0] + (b[0] - a[0]) * u,
        a[1] + (b[1] - a[1]) * u,
        a[2] + (b[2] - a[2]) * u,
    )


def _normalise(v: tuple[float, float, float]) -> tuple[float, float, float]:
    magnitude = math.sqrt(sum(component * component for component in v))
    return tuple(component / magnitude for component in v)  # type: ignore[return-value]


def build() -> list[tuple[float, float, float, float]]:
    """Return ``(t, x, y, z)`` rows for the scripted gesture sequence."""
    rows: list[tuple[float, float, float, float]] = []
    rest = (0.0, 0.0, 1.0)
    raised = (0.707, 0.0, 0.707)  # wrist pitched 45 degrees by the raise
    t = 0.0

    def hold(seconds: float, up: tuple[float, float, float], linear: float = 0.0) -> None:
        nonlocal t
        end = t + seconds
        while t < end:
            t = _push(rows, t, up, 1.0, linear)

    def swing(
        seconds: float,
        start: tuple[float, float, float],
        end_dir: tuple[float, float, float],
        linear: float,
    ) -> None:
        nonlocal t
        end = t + seconds
        while t < end:
            u = 0.0 if seconds <= 0 else (t - (end - seconds)) / seconds
            t = _push(rows, t, _normalise(_slerp(start, end_dir, u)), 1.0, linear)

    hold(2.0, rest)

    # Clap: a hard, very short spike with no change in wrist orientation.
    for _ in range(2):
        t = _push(rows, t, rest, 1.0, 2.4)
    hold(0.06, rest)
    for _ in range(2):
        t = _push(rows, t, rest, 1.0, -1.8)
    hold(2.0, rest)

    # Fast raise: the wrist pitches up while the hand accelerates upward.
    swing(0.30, rest, raised, linear=1.6)
    # Braking at the top of the movement, then holding it there.
    swing(0.20, raised, raised, linear=-1.2)
    hold(2.0, raised)

    # Fast lower: pitch back down with the hand accelerating downward.
    swing(0.30, raised, rest, linear=-1.6)
    swing(0.20, rest, rest, linear=1.2)
    hold(2.0, rest)

    return rows


def build_negatives() -> list[tuple[float, float, float, float]]:
    """Return rows for movements that must NOT be reported as gestures.

    Each block is separated by enough quiet time that a real gesture would have
    been able to fire, so anything printed for this scenario is a false
    positive from the shape tests, not a cooldown accident.
    """
    rows: list[tuple[float, float, float, float]] = []
    rest = (0.0, 0.0, 1.0)
    raised = (0.707, 0.0, 0.707)
    t = 0.0

    def hold(seconds: float, up: tuple[float, float, float], linear: float = 0.0) -> None:
        nonlocal t
        end = t + seconds
        while t < end:
            t = _push(rows, t, up, 1.0, linear)

    def shake(seconds: float, up: tuple[float, float, float], amplitude: float,
              hz: float) -> None:
        """Vertical vibration: strong acceleration, no change in heading."""
        nonlocal t
        end = t + seconds
        while t < end:
            wobble = amplitude * math.sin(2.0 * math.pi * hz * t)
            t = _push(rows, t, up, 0.0, wobble)

    hold(2.0, rest)

    # 1. Sustained clench / swung arm: high acceleration, but far too wide to
    #    be a clap, and with no pitch change it is no gesture either.
    hold(0.6, rest, linear=1.8)
    hold(2.0, rest)

    # 2. Vertical vibration (a shaking hand, a lawnmower): alternating vertical
    #    acceleration with the wrist held still.
    shake(1.2, rest, amplitude=1.4, hz=9.0)
    hold(2.0, rest)

    # 3. Slow, deliberate lift: the heading changes, but the leaky integrator
    #    should never reach the velocity threshold.
    swing_end = t + 2.0
    while t < swing_end:
        u = (t - (swing_end - 2.0)) / 2.0
        t = _push(rows, t, _normalise(_slerp(rest, raised, u)), 1.0, 0.25)
    hold(2.0, raised)

    # 4. Walking: small rhythmic vertical bounce, wrist roughly level.
    shake(2.0, rest, amplitude=0.35, hz=1.8)
    hold(1.0, rest)

    return rows


def to_rows(raw: list[tuple[float, float, float, float]]) -> list[Row]:
    from _motion_replay import Row  # noqa: PLC0415
    from custom_components.colmi_ring.colmi_codec.motion import AccelSample  # noqa: PLC0415

    return [
        Row(t=t, sample=AccelSample(x=x, y=y, z=z)) for t, x, y, z in raw
    ]


def write_csv(raw: list[tuple[float, float, float, float]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("t,x,y,z\n")
        for row in raw:
            handle.write(
                f"{row[0]:.4f},{row[1]:.5f},{row[2]:.5f},{row[3]:.5f}\n"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the stream here as CSV instead of only replaying it",
    )
    parser.add_argument("--sensitivity", type=float, default=1.0)
    parser.add_argument(
        "--no-stream-clap", action="store_true", help="disable the stream clap"
    )
    parser.add_argument(
        "--scenario",
        choices=("gestures", "negatives"),
        default="gestures",
        help="positive gestures (default) or the must-not-fire sequence",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw = build() if args.scenario == "gestures" else build_negatives()
    if args.out is not None:
        write_csv(raw, args.out)
        print(f"wrote {args.out} ({len(raw)} samples)")
        return 0

    from _motion_replay import report_gestures, report_signal, run_detector  # noqa: PLC0415

    rows = to_rows(raw)
    report_signal(rows)
    expectation = (
        "one clap, one hand_raise, one hand_lower"
        if args.scenario == "gestures"
        else "none at all"
    )
    print(f"\n## Events (expect {expectation})")
    fired = run_detector(rows, args.sensitivity, not args.no_stream_clap)
    report_gestures(fired)
    return 0


if __name__ == "__main__":
    sys.exit(main())
