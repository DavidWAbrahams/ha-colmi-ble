"""Smoke test for the per-model hole axis (temporary, not shipped)."""

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

from custom_components.colmi_ring.colmi_codec.motion import AccelSample  # noqa: E402
from custom_components.colmi_ring.const import resolve_hole_axis  # noqa: E402
from custom_components.colmi_ring.gestures import VerticalAngleTracker  # noqa: E402

checks = [
    ("R08_3090 -> y", resolve_hole_axis("auto", "R08_3090"), "y"),
    ("R8_3090 -> y", resolve_hole_axis("auto", "R8_3090"), "y"),
    ("R8 spaced -> y", resolve_hole_axis("auto", "R8 3090"), "y"),
    ("lowercase -> y", resolve_hole_axis("auto", "r08_3090"), "y"),
    ("R02_3090 -> z", resolve_hole_axis("auto", "R02_3090"), "z"),
    ("no name -> z", resolve_hole_axis("auto", None), "z"),
    ("empty title -> z", resolve_hole_axis("auto", ""), "z"),
    ("override x wins", resolve_hole_axis("x", "R08_3090"), "x"),
    ("override Y wins", resolve_hole_axis("Y", "R02_3090"), "y"),
    ("garbage option -> table", resolve_hole_axis("w", "R08_1"), "y"),
    ("garbage option, other ring -> z", resolve_hole_axis("w", "R02_1"), "z"),
    ("R080 name not matched", resolve_hole_axis("auto", "R080_3090"), "z"),
]
ok = True
for label, got, want in checks:
    status = "PASS" if got == want else "FAIL"
    ok = ok and got == want
    print(f"{status} {label}: {got!r}")

# An R08 held upright: gravity along -Y must read 90 with hole_axis="y".
tracker = VerticalAngleTracker(hole_axis="y")
for i in range(5):
    tracker.feed(AccelSample(0.0, -1.0, 0.0), now=float(i))
upright = tracker.angle_deg
print(f"R08 upright (gravity on -Y) reads {upright:.1f} (expect ~90)")
ok = ok and upright is not None and upright > 89.0

# The same ring laid flat: gravity along Z must read 0.
tracker = VerticalAngleTracker(hole_axis="y")
for i in range(5):
    tracker.feed(AccelSample(0.0, 0.0, 1.0), now=float(i))
flat = tracker.angle_deg
print(f"R08 flat (gravity on Z) reads {flat:.1f} (expect ~0)")
ok = ok and flat is not None and flat < 1.0

# And with the default (Z) axis the old behaviour is unchanged.
tracker = VerticalAngleTracker()
for i in range(5):
    tracker.feed(AccelSample(0.0, 0.0, -1.0), now=float(i))
z_up = tracker.angle_deg
print(f"default axis, gravity on -Z reads {z_up:.1f} (expect ~90)")
ok = ok and z_up is not None and z_up > 89.0

print("all hole axis smoke checks passed" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
