#!/usr/bin/env python3
"""Offline sanity checks for VerticalAngleTracker (temporary, not part of the package).

Run: python scripts/_vertical_angle_check.py
"""

from __future__ import annotations

import math
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
    the codec and the tracker are pure.
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
    VerticalAngleTracker,
)

RATE = 20.0
failures: list[str] = []


def dirs(*, elev_deg: float, yaw_deg: float = 0.0, g: float = 1.0) -> AccelSample:
    """Unit gravity direction for a Z axis elevated ``elev_deg`` above horizontal.

    ``yaw_deg`` spins the axis around the vertical; it must not change what the
    tracker reports, because the tracker reads only the Z component.
    """
    elev = math.radians(elev_deg)
    yaw = math.radians(yaw_deg)
    z = math.sin(elev)
    horizontal = math.cos(elev)
    return AccelSample(
        horizontal * math.sin(yaw) * g,
        horizontal * math.cos(yaw) * g,
        z * g,
    )


def run(tracker: VerticalAngleTracker, poses: list[AccelSample], t0: float) -> float:
    """Feed a list of poses at RATE; return the monotonic time after them."""
    for index, pose in enumerate(poses):
        tracker.feed(pose, now=t0 + index / RATE)
    return t0 + len(poses) / RATE


def hold(elev_deg: float, seconds: float, **kwargs: float) -> list[AccelSample]:
    base = dirs(elev_deg=elev_deg, **kwargs)
    return [base] * int(seconds * RATE)


def jitter(elev_deg: float, seconds: float) -> list[AccelSample]:
    """Resting hand: quantization-sized noise on each axis."""
    base = dirs(elev_deg=elev_deg)
    poses: list[AccelSample] = []
    for index in range(int(seconds * RATE)):
        wiggle = 0.002 * math.sin(index * 1.7)
        poses.append(
            AccelSample(base.x + wiggle, base.y + wiggle * 0.5, base.z + wiggle)
        )
    return poses


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


# 1. a ring held flat (Z axis horizontal) reads zero.
tracker = VerticalAngleTracker()
run(tracker, jitter(0.0, 2.0), t0=0.0)
check("flat reads 0", tracker.angle_deg == 0.0, f"angle={tracker.angle_deg}")

# 2. elevations read themselves.
tracker = VerticalAngleTracker()
for target in (30.0, 45.0, 60.0, 80.0):
    run(tracker, hold(target, 2.0), t0=100.0)
    check(
        f"elevation {target}",
        tracker.angle_deg is not None and abs(tracker.angle_deg - target) < 3.0,
        f"angle={tracker.angle_deg}",
    )

# 3. straight up and straight down both read 90 (no sign, no 180).
tracker = VerticalAngleTracker()
run(tracker, hold(90.0, 2.0), t0=200.0)
up = tracker.angle_deg
run(tracker, hold(-90.0, 2.0), t0=300.0)
down = tracker.angle_deg
check(
    "up and down both 90",
    up is not None
    and down is not None
    and abs(up - 90.0) < 3.0
    and abs(down - 90.0) < 3.0,
    f"up={up} down={down}",
)

# 4. yaw about gravity is invisible at a fixed elevation.
tracker = VerticalAngleTracker()
run(tracker, hold(45.0, 1.0), t0=400.0)
before = tracker.angle_deg
spin = [dirs(elev_deg=45.0, yaw_deg=index * 5.0) for index in range(int(RATE * 3))]
run(tracker, spin, t0=410.0)
check(
    "yaw does not move the reading",
    tracker.angle_deg is not None
    and before is not None
    and abs(tracker.angle_deg - before) < 1.0,
    f"before={before} after={tracker.angle_deg}",
)

# 5. a movement (magnitude away from 1 g) does not move the reading.
tracker = VerticalAngleTracker()
run(tracker, jitter(20.0, 2.0), t0=500.0)
before = tracker.angle_deg
run(tracker, hold(20.0, 2.0, g=2.5), t0=510.0)
check(
    "linear acceleration is ignored",
    tracker.angle_deg is not None
    and before is not None
    and tracker.angle_deg == before,
    f"before={before} after={tracker.angle_deg}",
)

# 6. calibrating is nothing: a fresh tracker answers on the first good sample.
fresh = VerticalAngleTracker()
first = fresh.feed(dirs(elev_deg=50.0), now=0.0)
check(
    "first trusted sample answers",
    first is not None and abs(first - 50.0) < 1.0,
    f"first={first}",
)

# 7. a gap in the stream must not move the reading.
tracker = VerticalAngleTracker()
run(tracker, jitter(25.0, 2.0), t0=600.0)
before = tracker.angle_deg
tracker.feed(dirs(elev_deg=25.0), now=2000.0)
check(
    "gap does not move the reading",
    tracker.angle_deg == before,
    f"before={before} after={tracker.angle_deg}",
)

# 8. a sample too weak to define gravity is ignored rather than believed.
tracker = VerticalAngleTracker()
run(tracker, jitter(35.0, 2.0), t0=700.0)
before = tracker.angle_deg
tracker.feed(AccelSample(0.05, 0.05, 0.05), now=702.0)
check(
    "weak sample ignored",
    tracker.angle_deg == before,
    f"before={before} after={tracker.angle_deg}",
)

# 9. reset clears, and the next good sample answers again immediately.
tracker.reset()
check("reset clears", tracker.angle_deg is None)
again = tracker.feed(dirs(elev_deg=10.0), now=800.0)
check(
    "reset then feed answers",
    again is not None and abs(again - 10.0) < 1.0,
    f"again={again}",
)

# 10. a slow sweep from flat to vertical tracks it, lagging by about one tau.
tracker = VerticalAngleTracker()
sweep_seconds = 40.0
samples = int(RATE * sweep_seconds)
step = 90.0 / samples
run(tracker, [dirs(elev_deg=index * step) for index in range(samples)], t0=900.0)
check(
    "sweep ends at 90",
    tracker.angle_deg is not None and abs(tracker.angle_deg - 90.0) < 2.0,
    f"angle={tracker.angle_deg}",
)

# 11. a step from vertical to flat settles quickly, not at the slow-tau pace.
# With a single 0.5 s tau a 90->0 step still reads ~33 after 0.5 s; the
# adaptive snap must bring that under 10, and to rest within ~2 s.
tracker = VerticalAngleTracker()
run(tracker, hold(90.0, 1.0), t0=1000.0)
run(tracker, hold(0.0, 0.5), t0=1010.0)
settled_fast = tracker.angle_deg
check(
    "step 90->0 mostly settled after 0.5 s",
    settled_fast is not None and settled_fast < 10.0,
    f"angle={settled_fast}",
)
run(tracker, hold(0.0, 1.5), t0=1010.5)
settled = tracker.angle_deg
check(
    "step 90->0 at rest after 2 s",
    settled is not None and settled < 1.5,
    f"angle={settled}",
)

if failures:
    print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
    raise SystemExit(1)
print("\nall vertical angle checks passed")
