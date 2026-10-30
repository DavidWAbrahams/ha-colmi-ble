#!/usr/bin/env python3
"""Offline sanity checks for TiltTracker (temporary, not part of the package).

Run: python scripts/_tilt_check.py
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _bootstrap_package(name: str, path: Path) -> None:
    """Register *name* as a package without running its HA-dependent init."""
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


if "custom_components.colmi_ring" not in sys.modules:
    _bootstrap_package("custom_components", ROOT / "custom_components")
    _bootstrap_package(
        "custom_components.colmi_ring", ROOT / "custom_components" / "colmi_ring"
    )

from custom_components.colmi_ring.colmi_codec.motion import (  # noqa: E402
    AccelSample,
)
from custom_components.colmi_ring.gestures import TiltTracker  # noqa: E402

RATE = 25.0


def dirs(*, tilt_deg: float, yaw_deg: float = 0.0) -> AccelSample:
    """Unit gravity direction for a hand tilted ``tilt_deg`` from +Z, then yawed."""
    theta = math.radians(tilt_deg)
    x = math.sin(theta)
    z = math.cos(theta)
    y = 0.0
    # yaw about gravity is invisible, so rotate in the ring frame instead
    phi = math.radians(yaw_deg)
    return AccelSample(
        x * math.cos(phi) - y * math.sin(phi),
        x * math.sin(phi) + y * math.cos(phi),
        z,
    )


def run(tracker: TiltTracker, poses: list[AccelSample], t0: float = 0.0) -> list[float | None]:
    out: list[float | None] = []
    for index, pose in enumerate(poses):
        out.append(tracker.feed(pose, now=t0 + index / RATE))
    return out


def hold(tilt_deg: float, seconds: float, **kwargs: float) -> list[AccelSample]:
    base = dirs(tilt_deg=tilt_deg, **kwargs)
    count = int(seconds * RATE)
    return [
        AccelSample(
            base.x * (1.0 + 0.01 * math.sin(index * 1.7)),
            base.y * (1.0 + 0.01 * math.cos(index * 2.3)),
            base.z * (1.0 + 0.01 * math.sin(index * 3.1)),
        )
        for index in range(count)
    ]


def jitter(tilt_deg: float, seconds: float) -> list[AccelSample]:
    """Resting hand: quantization-sized noise on each axis."""
    base = dirs(tilt_deg=tilt_deg)
    count = int(seconds * RATE)
    out: list[AccelSample] = []
    for index in range(count):
        noise = 0.002 * math.sin(index * 5.0)
        out.append(AccelSample(base.x + noise, base.y - noise, base.z + noise * 0.5))
    return out


failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# 1. zeroing from rest, then holding still, reads zero.
tracker = TiltTracker()
angles = run(tracker, jitter(0.0, 3.0))
check("still -> zeroed", tracker.calibrated)
check("still -> reads zero", tracker.angle_deg == 0.0, f"angle={tracker.angle_deg}")
check("unzeroed before rest", angles[0] is None, f"first={angles[0]}")

# 2. a 45 degree tilt reads about 45.
run(tracker, hold(45.0, 2.0))
check("tilt 45", tracker.angle_deg is not None and abs(tracker.angle_deg - 45.0) < 3.0,
      f"angle={tracker.angle_deg}")

# 3. returning home returns the reading (no drift).
run(tracker, jitter(0.0, 3.0))
check("home again", tracker.angle_deg is not None and tracker.angle_deg < 3.0,
      f"angle={tracker.angle_deg}")

# 4. resting noise does not wander.
worst = 0.0
for pose in jitter(0.0, 5.0):
    tracker.feed(pose, now=100.0)
    worst = max(worst, tracker.angle_deg or 0.0)
worst = 0.0
tracker2 = TiltTracker()
run(tracker2, jitter(0.0, 2.0))
values = [a for a in run(tracker2, jitter(0.0, 5.0)) if a is not None]
check(
    "quiet hand stays flat",
    max(values) <= 2.0,
    f"max={max(values):.2f} over {len(values)} samples",
)

# 5. yaw about gravity is invisible (documented limitation).
tracker = TiltTracker()
run(tracker, jitter(0.0, 2.0))
run(tracker, hold(0.0, 2.0, yaw_deg=90.0))
check(
    "yaw invisible",
    tracker.angle_deg is not None and tracker.angle_deg < 3.0,
    f"angle={tracker.angle_deg}",
)

# 6. a movement (magnitude away from 1 g) does not fake a tilt.
tracker = TiltTracker()
run(tracker, jitter(0.0, 2.0))
rushing = [AccelSample(0.2, 0.1, 3.0) for _ in range(int(RATE * 2))]
run(tracker, rushing)
check(
    "linear acceleration is not tilt",
    tracker.angle_deg is not None and tracker.angle_deg < 3.0,
    f"angle={tracker.angle_deg}",
)

# 7. calibrating mid-session re-zeros wherever the hand is.
tracker = TiltTracker()
run(tracker, jitter(0.0, 2.0))
run(tracker, hold(60.0, 2.0))
check("calibrate: works", tracker.zero())
check("calibrate: reads zero", tracker.angle_deg == 0.0, f"angle={tracker.angle_deg}")
run(tracker, jitter(60.0, 2.0))
check("calibrate: holds", tracker.angle_deg is not None and tracker.angle_deg < 3.0,
      f"angle={tracker.angle_deg}")
run(tracker, hold(30.0, 2.0))
check(
    "calibrate: now relative to new zero",
    tracker.angle_deg is not None and abs(tracker.angle_deg - 30.0) < 4.0,
    f"angle={tracker.angle_deg}",
)

# 8. a hand that never rests still is still zeroed, eventually.
tracker = TiltTracker()
sweeping = [dirs(tilt_deg=index * 0.05) for index in range(int(RATE * 12))]
run(tracker, sweeping)
check("busy hand gets zeroed", tracker.calibrated)
check("busy hand has a reading", tracker.angle_deg is not None)

# 9. a slow sweep defines a pose somewhere in the middle of itself.
tracker = TiltTracker()
run(tracker, sweeping)
check(
    "slow sweep is not read as rest",
    tracker.angle_deg is None or tracker.angle_deg > 0.0,
    f"angle={tracker.angle_deg}",
)

# 10. a gap in the stream must not move the reading.
tracker = TiltTracker()
run(tracker, jitter(0.0, 2.0))
before = tracker.angle_deg
tracker.feed(dirs(tilt_deg=0.0), now=1000.0)
check(
    "gap held",
    tracker.angle_deg == before,
    f"before={before} after={tracker.angle_deg}",
)

# 11. upside down reads 180, not 0 (no folding).
tracker = TiltTracker()
run(tracker, jitter(0.0, 2.0))
run(tracker, hold(180.0, 3.0))
check(
    "inverted reads 180",
    tracker.angle_deg is not None and tracker.angle_deg > 170.0,
    f"angle={tracker.angle_deg}",
)

# 12. a sample too weak to define gravity is ignored rather than believed.
tracker = TiltTracker()
run(tracker, jitter(0.0, 2.0))
before = tracker.angle_deg
run(tracker, [AccelSample(0.01, 0.0, 0.0)] * int(RATE))
check("free fall ignored", tracker.angle_deg == before, f"angle={tracker.angle_deg}")

# 13. reset() forgets the zero.
tracker.reset()
check("reset unzeroes", (not tracker.calibrated) and tracker.angle_deg is None)
check("reset then feed is None", tracker.feed(dirs(tilt_deg=10.0), now=0.0) is None)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("all tilt checks passed")
