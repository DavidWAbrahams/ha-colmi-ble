#!/usr/bin/env python3
"""Offline sanity checks for StreamGestureDetector (temporary, dev-only).

Run: python scripts/_flip_check.py

Simulates the two stream gestures the way the real stream behaves -- 1 Hz
(stock R02 firmware) and 4 Hz (fast firmware) -- and asserts:

* stillness fires once per calm episode, exactly ``hold_s`` in, and re-arms
  only after the hand actually moves;
* a flip (two confirmed poses >= 120 degrees apart with a fast transition)
  fires once, at the end of the second hold, at both sample rates;
* negatives stay negative: slow rotation, a stream gap, and a chain of
  orthogonal (90 degree) poses must never report a flip;
* the diagnostics MotionManager logs (sample rate, rest magnitude) still
  report sane values.

Nothing here imports Home Assistant; the bootstrap trick comes straight from
``scripts/_motion_replay.py``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))


def _bootstrap_package(name: str, path: Path) -> types.ModuleType:
    """Register *name* as a package without executing its ``__init__``.

    Same trick as ``scripts/_motion_replay.py``: the integration's ``__init__``
    imports ``homeassistant``, which a plain dev checkout does not have, while
    the codec and the detector are pure.
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

from custom_components.colmi_ring.colmi_codec.motion import (  # noqa: E402
    AccelSample,
)
from custom_components.colmi_ring.gestures import (  # noqa: E402
    GESTURE_FLIP,
    GESTURE_STILL,
    SOURCE_STREAM,
    GestureEvent,
    StreamGestureDetector,
    _separation_deg,
)

HOLD_S = 2.0
failures: list[str] = []

UP = AccelSample(0.0, 0.0, 1.0)  # palm up
DOWN = AccelSample(0.0, 0.0, -1.0)  # palm down: 180 degrees from UP
SIDE = AccelSample(1.0, 0.0, 0.0)  # 90 degrees from both palm poses
WAVE_A = AccelSample(0.9, 0.0, 0.9)  # |a| = 1.27 g: clearly accelerating
WAVE_B = AccelSample(-0.9, 0.0, -0.9)  # alternates so nothing ever settles


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


def held(pose: AccelSample, seconds: float, rate: float) -> list[AccelSample]:
    """``seconds`` of one stationary pose at ``rate`` samples per second."""
    return [pose] * round(seconds * rate)


def waving(seconds: float, rate: float) -> list[AccelSample]:
    """``seconds`` of shaking: alternating vectors that never settle down."""
    return [
        WAVE_A if index % 2 == 0 else WAVE_B
        for index in range(round(seconds * rate))
    ]


def stream(
    detector: StreamGestureDetector,
    samples: list[AccelSample],
    rate: float,
    t0: float = 0.0,
) -> list[tuple[GestureEvent, float]]:
    """Feed samples spaced 1/rate apart; return ``(event, time)`` pairs."""
    fired: list[tuple[GestureEvent, float]] = []
    dt = 1.0 / rate
    for index, sample in enumerate(samples):
        now = t0 + index * dt
        fired.extend((event, now) for event in detector.feed(sample, now=now))
    return fired


def times_of(
    fired: list[tuple[GestureEvent, float]], gesture: str
) -> list[float]:
    return [now for event, now in fired if event.gesture == gesture]


def events_of(
    fired: list[tuple[GestureEvent, float]], gesture: str
) -> list[GestureEvent]:
    return [event for event, _ in fired if event.gesture == gesture]


# 1. Stillness: one event per calm episode, ``HOLD_S`` in, re-armed by motion.
detector = StreamGestureDetector(hold_s=HOLD_S)
fired = stream(
    detector,
    held(UP, 6.0, 1.0) + waving(3.0, 1.0) + held(UP, 4.0, 1.0),
    rate=1.0,
)
stills = times_of(fired, GESTURE_STILL)
check(
    "still fires once per calm episode",
    len(stills) == 2,
    f"times={stills}",
)
check(
    "still fires at hold_s, not earlier",
    bool(stills) and abs(stills[0] - HOLD_S) < 1e-9,
    f"first={stills[:1]}",
)
check(
    "still re-arms after movement",
    len(stills) == 2 and abs(stills[1] - 11.0) < 1e-9,
    f"times={stills}",
)
check(
    "no flip without a pose change",
    not times_of(fired, GESTURE_FLIP),
)

# 2. Flip at 1 Hz: palm up 3 s, one shaking sample, palm down 3 s.
detector = StreamGestureDetector(hold_s=HOLD_S)
fired = stream(
    detector,
    held(UP, 3.0, 1.0) + waving(1.0, 1.0) + held(DOWN, 3.0, 1.0),
    rate=1.0,
)
flips = times_of(fired, GESTURE_FLIP)
check("flip fires at 1 Hz", len(flips) == 1, f"times={flips}")
if flips:
    flip = events_of(fired, GESTURE_FLIP)[0]
    check(
        "flip payload: source and angle",
        flip.source == SOURCE_STREAM and abs(flip.angle_deg - 180.0) < 1e-9,
        f"source={flip.source} angle={flip.angle_deg}",
    )
    check(
        "flip payload: hold seconds",
        abs(flip.hold_s - HOLD_S) < 1e-9,
        f"hold_s={flip.hold_s}",
    )
    check(
        "flip arrives at the end of the second hold",
        abs(flips[0] - 6.0) < 1e-9,
        f"t={flips[0]}",
    )
else:
    check("flip payload: source and angle", False, "no flip fired")
    check("flip payload: hold seconds", False, "no flip fired")
    check("flip arrives at the end of the second hold", False, "no flip fired")

# 3. Negative: the same two poses reached slowly must not read as a flip.
detector = StreamGestureDetector(hold_s=HOLD_S)
fired = stream(
    detector,
    held(UP, 3.0, 1.0) + waving(8.0, 1.0) + held(DOWN, 3.0, 1.0),
    rate=1.0,
)
check(
    "slow rotation confirms a new pose without flipping",
    not times_of(fired, GESTURE_FLIP),
)

# 4. Negative: a stream gap (reconnect) makes the transition unknowable.
detector = StreamGestureDetector(hold_s=HOLD_S)
fired = stream(detector, held(UP, 3.0, 1.0), rate=1.0)
fired += stream(detector, held(DOWN, 3.0, 1.0), rate=1.0, t0=23.0)
check(
    "no flip across a long stream gap",
    not times_of(fired, GESTURE_FLIP),
)

# 5. Back-to-back flips each fire: every confirmed pose anchors the next one.
detector = StreamGestureDetector(hold_s=HOLD_S)
fired = stream(
    detector,
    held(UP, 3.0, 1.0)
    + waving(1.0, 1.0)
    + held(DOWN, 3.0, 1.0)
    + waving(1.0, 1.0)
    + held(UP, 3.0, 1.0),
    rate=1.0,
)
flips = times_of(fired, GESTURE_FLIP)
check(
    "back-to-back flips both fire",
    len(flips) == 2 and abs(flips[0] - 6.0) < 1e-9 and abs(flips[1] - 10.0) < 1e-9,
    f"times={flips}",
)

# 6. Flip at 4 Hz: the fixed 1 s transition floor binds instead of the dt term.
detector = StreamGestureDetector(hold_s=HOLD_S)
fired = stream(
    detector,
    held(UP, 2.5, 4.0) + waving(0.5, 4.0) + held(DOWN, 2.5, 4.0),
    rate=4.0,
)
flips = times_of(fired, GESTURE_FLIP)
check("flip fires at 4 Hz", len(flips) == 1, f"times={flips}")

# 7. Negative: a chain of orthogonal poses (90 deg each) is not a flip.
detector = StreamGestureDetector(hold_s=HOLD_S)
fired = stream(
    detector,
    held(UP, 3.0, 1.0)
    + waving(1.0, 1.0)
    + held(SIDE, 3.0, 1.0)
    + waving(1.0, 1.0)
    + held(DOWN, 3.0, 1.0),
    rate=1.0,
)
check(
    "orthogonal poses never flip",
    not times_of(fired, GESTURE_FLIP),
)

# 8. Stream diagnostics used by MotionManager's once-per-session log.
rate_hz = detector.sample_rate_hz
magnitude = detector.rest_magnitude_g()
check(
    "sample_rate_hz reads ~1 Hz",
    rate_hz is not None and 0.9 <= rate_hz <= 1.1,
    f"rate={rate_hz}",
)
check(
    "rest_magnitude_g reads ~1 g",
    magnitude is not None and 0.9 <= magnitude <= 1.2,
    f"magnitude={magnitude}",
)

# 9. Regression guard: pose separation must NOT fold 180 degrees to 0 the way
# the old abs()-based angle helper did -- a palm flip *is* that 180 degrees.
up_down = _separation_deg(UP, DOWN)
up_side = _separation_deg(UP, SIDE)
check(
    "separation keeps its sign budget: up vs down is 180",
    abs(up_down - 180.0) < 1e-9,
    f"deg={up_down}",
)
check(
    "separation: up vs side is 90",
    abs(up_side - 90.0) < 1e-6,
    f"deg={up_side}",
)


def main() -> int:
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
