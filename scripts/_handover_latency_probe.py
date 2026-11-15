"""Measure handover latency after an incumbent goes silent (defaults only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _sticky_source_sim as s  # noqa: E402


def scenario(rssi_b: int) -> float | None:
    c = s.Credit()
    t = 0.0
    # 60 s of steady co-coverage, A incumbent at -50, B at rssi_b, 1 adv/s.
    while t < 60:
        c.note("A", -50, t)
        c.note("B", rssi_b, t)
        t += 1.0
    assert c.source == "A", c.source
    first: float | None = None
    # A goes completely silent; B keeps hearing every advertisement.
    while t < 120:
        before = c.source
        c.note("B", rssi_b, t)
        if before != c.source and first is None:
            first = t - 60.0
        t += 1.0
    return first


print(f"B at -55 (within margin):  handover {scenario(-55)} s after silence")
print(f"B at -90 (much weaker):    handover {scenario(-90)} s after silence")
