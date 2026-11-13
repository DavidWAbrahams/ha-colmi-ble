"""Offline sim of the coordinator's proximity credit ("Last Seen By" v2).

Mirrors ColmiRingDataUpdateCoordinator._async_credit_proxy so the scoring
behaviour can be checked without booting Home Assistant. If the coordinator
changes, change this too: the flip-flop stress case is the regression test
that the margin + ratio guards alone hold without a dwell lock.

Run: python scripts/_sticky_source_sim.py
"""

import random
from collections import deque
from dataclasses import dataclass, field
from statistics import median

# Mirrors const.py
WINDOW_S = 30.0
MARGIN_DB = 6
COUNT_RATIO = 1.25
MIN_SAMPLES = 3
RSSI_FLOOR_DB = -127
RSSI_CEIL_DB = -20


@dataclass
class _ProxySamples:
    stamps: deque[float] = field(default_factory=deque)
    signals: deque[int | None] = field(default_factory=deque)


class Credit:
    """The coordinator logic, ported literally (no hass, no callbacks)."""

    def __init__(self) -> None:
        self.stats: dict[str, _ProxySamples] = {}
        self.source: str | None = None
        self.rssi: int | None = None

    def note(self, source: str, rssi: int | None, now: float) -> bool:
        stats = self.stats.setdefault(source, _ProxySamples())
        stats.stamps.append(now)
        usable = (
            rssi if rssi is not None and RSSI_FLOOR_DB < rssi <= RSSI_CEIL_DB else None
        )
        stats.signals.append(usable)
        winner, score = self._credit(now)
        changed = winner != self.source
        self.source, self.rssi = winner, score
        return changed

    def _credit(self, now: float) -> tuple[str | None, int | None]:
        for src in list(self.stats):
            sample = self.stats[src]
            while sample.stamps and now - sample.stamps[0] > WINDOW_S:
                sample.stamps.popleft()
                sample.signals.popleft()
            if not sample.stamps:
                del self.stats[src]

        qualified = {
            src: s for src, s in self.stats.items() if len(s.stamps) >= MIN_SAMPLES
        }
        current = self.source
        if not qualified:
            return current, self.rssi

        def signal_of(sample: _ProxySamples) -> float | None:
            valid = [val for val in sample.signals if val is not None]
            return median(valid) if valid else None

        incumbent = qualified.get(current)
        if incumbent is None:
            best_src = max(
                qualified,
                key=lambda src: (
                    signal_of(qualified[src]) is not None,
                    signal_of(qualified[src]) or float("-inf"),
                    len(qualified[src].stamps),
                ),
            )
            best = qualified[best_src]
            score = signal_of(best)
            return best_src, round(score) if score is not None else None

        inc_signal = signal_of(incumbent)
        inc_count = len(incumbent.stamps)
        challenger: str | None = None
        challenger_sample: _ProxySamples | None = None
        for src, sample in qualified.items():
            if src == current:
                continue
            count = len(sample.stamps)
            signal = signal_of(sample)
            if inc_signal is None or signal is None:
                beats = count >= inc_count * COUNT_RATIO
            else:
                beats = signal >= inc_signal + MARGIN_DB or (
                    abs(signal - inc_signal) < MARGIN_DB
                    and count >= inc_count * COUNT_RATIO
                )
            if beats and (
                challenger_sample is None
                or (signal or float("-inf"))
                > (signal_of(challenger_sample) or float("-inf"))
            ):
                challenger, challenger_sample = src, sample
        if challenger is None or challenger_sample is None:
            return current, (round(inc_signal) if inc_signal is not None else None)
        score = signal_of(challenger_sample)
        return challenger, round(score) if score is not None else None


def run(name: str, feed, want: str | list, c: Credit | None = None) -> int:
    """Feed (t, src, rssi) tuples; assert final credit, return handover count."""
    c = c or Credit()
    handovers = 0
    for t, src, rssi in feed:
        if c.note(src, rssi, t):
            handovers += 1
    ok = c.source in (want if isinstance(want, list) else [want])
    print(
        f"{'PASS' if ok else 'FAIL'} {name}: credit={c.source!r} handovers={handovers}"
    )
    assert ok, f"{name}: expected {want!r}, got {c.source!r}"
    return handovers


# 1. User example 1: equal RSSI, A heard 30x vs B 20x in 30 s -> A wins.
feed = []
for i in range(60):
    t = i * 0.5
    feed.append((t, "A", -50))
    if i % 3 != 0:  # B misses a third of them
        feed.append((t, "B", -50))
run("example1 count tiebreak", feed, "A")

# 2. User example 2: equal counts, A -100 vs B -80 -> B wins.
feed = []
for i in range(60):
    t = i * 0.5
    feed.append((t, "A", -100))
    feed.append((t, "B", -80))
run("example2 rssi wins", feed, "B")

# 3. Flip-flop stress: identical proxies, seeded ±5 dB noise, 10 minutes.
#    Rules must hold: exactly 1 handover (the initial pick), zero after.
rng = random.Random(42)
feed = []
for i in range(1200):  # 10 min at 2 adv/s
    t = i * 0.5
    feed.append((t, "A", round(-60 + rng.uniform(-5, 5))))
    feed.append((t, "B", round(-60 + rng.uniform(-5, 5))))
handovers = run("flip-flop stress", feed, ["A", "B"])
assert handovers == 1, f"noise caused {handovers - 1} handovers beyond the pick"

# 4. Walk-out: A strong, then only B from t=60 (A falls below the gate as
#    its window ages out). One handover to B expected.
feed = []
for i in range(240):
    t = i * 0.5
    if t < 60:
        feed.append((t, "A", -50))
    feed.append((t, "B", -90))
run("walk-out handover", feed, "B")

# 5. 127 sentinel / 0 junk: sightings count, RSSI ignored; A still wins by
#    count, and never crashes on unusable values.
feed = []
for i in range(60):
    t = i * 0.5
    feed.append((t, "A", 127))  # sentinel -> counted, no signal
    feed.append((t, "A", -55))
    feed.append((t, "B", -55))
run("sentinel filtered", feed, "A")

# 6. Out of range entirely: credit retained, no crash, no handover.
c = Credit()
for i in range(60):
    c.note("A", -50, i * 0.5)
before = c.source
for i in range(120):  # 60 s with no sightings at all
    assert c._credit(30 + i * 0.5)[0] == before
print(f"PASS retained when gone: credit stays {before!r}")

print("\nall proximity-credit sim cases passed")
