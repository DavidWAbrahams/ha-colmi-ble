#!/usr/bin/env python3
"""
Offline sanity checks for the hand chop gesture (temporary, dev-only).

Run: python scripts/_chop_check.py

The chop is a raised pose that becomes flat: the hole axis must be held above
``MOTION_CHOP_RAISED_MIN_DEG`` for the chop hold to arm the gesture, then land
below ``MOTION_CHOP_FLAT_MAX_DEG`` - quickly, by the flip's rate-scaled
transition window - and be held there for the chop hold. Like
``scripts/_flip_check.py`` this simulates 1 Hz (stock R02 firmware) and 4 Hz
(fast firmware), and asserts:

* the chop fires once per move, at the end of the flat hold, at both rates;
* the event payload reports the flat hold length and elevation;
* negatives stay negative: flat without ever raising, a *slow* lowering
  through the gap between the bands, and a raised pose that never drops all
  the way must never fire;
* back-to-back chops each fire: firing disarms, and re-raising re-arms.

Nothing here imports Home Assistant; the bootstrap trick comes straight from
``scripts/_flip_check.py``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT))


def _bootstrap_package(name: str, path: Path) -> types.ModuleType:
    """Register *name* as a package without executing its ``__init__``."""
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
    GESTURE_CHOP,
    GESTURE_STILL,
    GestureEvent,
    StreamGestureDetector,
)

CHOP_HOLD_S = 2.0
# Long enough that the other stream gestures stay out of these captures, so
# every assertion below is about the chop alone.
OTHER_HOLD_S = 60.0
failures: list[str] = []

UP = AccelSample(0.0, 0.0, 1.0)  # hole axis vertical: elevation 90
SIDE = AccelSample(1.0, 0.0, 0.0)  # hole axis horizontal: elevation 0
MID = AccelSample(0.707, 0.0, 0.707)  # elevation ~45: the gap between bands


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


def held(pose: AccelSample, seconds: float, rate: float) -> list[AccelSample]:
    """``seconds`` of one stationary pose at ``rate`` samples per second."""
    return [pose] * round(seconds * rate)


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


def times_of(fired: list[tuple[GestureEvent, float]], gesture: str) -> list[float]:
    return [now for event, now in fired if event.gesture == gesture]


def events_of(
    fired: list[tuple[GestureEvent, float]], gesture: str
) -> list[GestureEvent]:
    return [event for event, _ in fired if event.gesture == gesture]


def new_detector() -> StreamGestureDetector:
    return StreamGestureDetector(
        still_hold_s=OTHER_HOLD_S,
        flip_hold_s=OTHER_HOLD_S,
        raised_hold_s=OTHER_HOLD_S,
        chop_hold_s=CHOP_HOLD_S,
    )


def chop_sequence(
    seconds_up: float, seconds_flat: float, rate: float
) -> list[AccelSample]:
    """Raised hold, instant drop (one shaking sample), flat hold."""
    shake = AccelSample(0.9, 0.0, 0.9)  # 1.27 g: the drop is never calm
    return held(UP, seconds_up, rate) + [shake] + held(SIDE, seconds_flat, rate)


# 1. Chop at 1 Hz: up 4 s, drop, flat 4 s -> fires once, at the flat hold end.
det = new_detector()
fired = stream(det, chop_sequence(4.0, 4.0, 1.0), rate=1.0)
chops = times_of(fired, GESTURE_CHOP)
check("chop fires at 1 Hz", len(chops) == 1, f"times={chops}")
if chops:
    event = events_of(fired, GESTURE_CHOP)[0]
    check(
        "chop reports its flat hold and elevation",
        event.hold_s >= CHOP_HOLD_S and event.angle_deg < 1.0,
        f"hold_s={event.hold_s} angle_deg={event.angle_deg}",
    )
    check("no stillness event stole the capture", not times_of(fired, GESTURE_STILL))

# 2. Chop at 4 Hz: same move, four times the samples.
det = new_detector()
fired = stream(det, chop_sequence(4.0, 4.0, 4.0), rate=4.0)
chops = times_of(fired, GESTURE_CHOP)
check("chop fires at 4 Hz", len(chops) == 1, f"times={chops}")

# 3. Negative: flat without ever raising arms nothing.
det = new_detector()
fired = stream(det, held(SIDE, 8.0, 1.0), rate=1.0)
check(
    "flat alone is not a chop",
    not times_of(fired, GESTURE_CHOP),
    f"times={times_of(fired, GESTURE_CHOP)}",
)

# 4. Negative: the same two poses reached *slowly* (3 s parked at 45 degrees,
# inside the gap between the bands) must age the transition window out.
det = new_detector()
slow = held(UP, 4.0, 1.0) + held(MID, 3.0, 1.0) + held(SIDE, 4.0, 1.0)
fired = stream(det, slow, rate=1.0)
check(
    "a slow lowering is not a chop",
    not times_of(fired, GESTURE_CHOP),
    f"times={times_of(fired, GESTURE_CHOP)}",
)

# 5. Negative: raised, then only lowered to the gap (never flat).
det = new_detector()
fired = stream(det, held(UP, 4.0, 1.0) + held(MID, 6.0, 1.0), rate=1.0)
check(
    "halfway down is not a chop",
    not times_of(fired, GESTURE_CHOP),
    f"times={times_of(fired, GESTURE_CHOP)}",
)

# 6. Negative: a raised hold *shorter* than the chop hold never arms.
det = new_detector()
fired = stream(det, chop_sequence(1.0, 4.0, 1.0), rate=1.0)
check(
    "a barely-raised hand cannot chop",
    not times_of(fired, GESTURE_CHOP),
    f"times={times_of(fired, GESTURE_CHOP)}",
)

# 7. Back-to-back chops each fire: firing disarms, re-raising re-arms.
det = new_detector()
double = chop_sequence(4.0, 4.0, 1.0) + chop_sequence(4.0, 4.0, 1.0)
fired = stream(det, double, rate=1.0)
chops = times_of(fired, GESTURE_CHOP)
check("two chops, two events", len(chops) == 2, f"times={chops}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("all chop checks passed")
