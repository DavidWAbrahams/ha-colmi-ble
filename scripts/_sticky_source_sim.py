"""Offline sim of the coordinator's proximity credit ("Seen by", top-K vote).

Mirrors ColmiRingDataUpdateCoordinator._async_credit_proxy (and the const.py
clamps) so the vote behaviour can be checked without booting Home Assistant.
If the coordinator changes, change this too: the sparse-capture and lucky-
spike cases are the regressions this algorithm exists to fix - proxies
time-share BLE with WiFi, so capture counts are noisy and RSSI-primary
voting with an incumbent margin must stay put.

Run: python scripts/_sticky_source_sim.py
"""

import random
from collections import Counter, deque
from dataclasses import dataclass

# Mirrors const.py
DEFAULT_SAMPLES_N = 45
DEFAULT_TOP_K = 12
DEFAULT_VOTE_MARGIN = 2
MAX_AGE_S = 300.0
RSSI_FLOOR_DB = -127
RSSI_CEIL_DB = -20

N_MIN, N_MAX = 10, 300
K_MIN, K_MAX = 3, 100
M_MIN, M_MAX = 0, 20


def clamp_samples_n(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_SAMPLES_N
    return max(N_MIN, min(N_MAX, number))


def clamp_top_k(value: object, samples_n: int) -> int:
    try:
        k = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        k = DEFAULT_TOP_K
    k = max(K_MIN, min(K_MAX, k))
    return min(k, max(K_MIN, samples_n))


def clamp_margin(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_VOTE_MARGIN
    return max(M_MIN, min(M_MAX, number))


@dataclass(frozen=True, slots=True)
class Sighting:
    when: float
    source: str
    signal: int | None


class Credit:
    """The coordinator logic, ported literally (no hass, no callbacks)."""

    def __init__(
        self,
        samples_n: object = DEFAULT_SAMPLES_N,
        top_k: object = DEFAULT_TOP_K,
        vote_margin: object = DEFAULT_VOTE_MARGIN,
    ) -> None:
        self.samples_n = samples_n
        self.top_k = top_k
        self.vote_margin = vote_margin
        self.pool: deque[Sighting] = deque()
        self.source: str | None = None
        self.rssi: int | None = None
        # (time, new winner) for every credit change, for handover assertions.
        self.flips: list[tuple[float, str]] = []

    def note(self, source: str, rssi: int | None, now: float) -> bool:
        usable = (
            rssi if rssi is not None and RSSI_FLOOR_DB < rssi <= RSSI_CEIL_DB else None
        )
        self.pool.append(Sighting(when=now, source=source, signal=usable))
        winner, score = self._credit(now)
        changed = winner != self.source
        if changed and winner is not None:
            self.flips.append((now, winner))
        self.source, self.rssi = winner, score
        return changed

    def _credit(self, now: float) -> tuple[str | None, int | None]:
        pool_size = clamp_samples_n(self.samples_n)
        top_k = clamp_top_k(self.top_k, pool_size)
        margin = clamp_margin(self.vote_margin)

        pool = self.pool
        while pool and (now - pool[0].when > MAX_AGE_S or len(pool) > pool_size):
            pool.popleft()
        if not pool:
            return self.source, self.rssi

        top = sorted(
            pool,
            key=lambda s: (
                s.signal is not None,
                s.signal if s.signal is not None else 0,
            ),
            reverse=True,
        )[:top_k]
        votes = Counter(sighting.source for sighting in top)

        best: dict[str, int] = {}
        for sighting in top:
            if sighting.signal is None:
                continue
            stored = best.get(sighting.source)
            if stored is None or sighting.signal > stored:
                best[sighting.source] = sighting.signal

        def fresh_signal_of(source: str) -> int | None:
            for sighting in reversed(pool):
                if sighting.source == source and sighting.signal is not None:
                    return sighting.signal
            return None

        current = self.source
        if current in votes:
            incumbent_votes = votes[current]
            contenders = [
                (src, count)
                for src, count in votes.items()
                if src != current and count >= incumbent_votes + margin
            ]
            if not contenders:
                return current, fresh_signal_of(current)
            winner = max(
                contenders,
                key=lambda sc: (sc[1], best.get(sc[0], RSSI_FLOOR_DB)),
            )[0]
        else:
            winner = max(
                votes,
                key=lambda src: (votes[src], best.get(src, RSSI_FLOOR_DB)),
            )
        return winner, fresh_signal_of(winner)


def run(
    name: str, feed, want: str | list, c: Credit | None = None
) -> tuple[int, Credit]:
    """Feed (t, src, rssi) tuples chronologically; assert credit, return stats."""
    c = c or Credit()
    handovers = 0
    for t, src, rssi in sorted(feed):
        if c.note(src, rssi, t):
            handovers += 1
    ok = c.source in (want if isinstance(want, list) else [want])
    print(
        f"{'PASS' if ok else 'FAIL'} {name}: credit={c.source!r} "
        f"rssi={c.rssi} handovers={handovers}"
    )
    assert ok, f"{name}: expected {want!r}, got {c.source!r}"
    return handovers, c


def settle(c: Credit, home: str) -> float:
    """
    Assert the credit rests on ``home``: its final flip must be *to* home.

    Returns the time of that final acquisition. Anything other than a flip
    away and back (a flip-flop) leaves a post-settle flip in the list, so a
    clean settle plus an early settle time is the whole stability property.
    """
    last_home = None
    for i, (_, src) in enumerate(c.flips):
        if src == home:
            last_home = i
    if last_home is None or last_home != len(c.flips) - 1:
        raise AssertionError(f"credit did not settle on {home!r}: {c.flips}")
    return c.flips[last_home][0]


def captures(
    rng: random.Random, source: str, base_db: float, gap_s: float, end_s: float
) -> list[tuple[float, str, int]]:
    """A proxy hearing the ring every gap_s (±50%) with base_db (±5)."""
    out = []
    t = rng.uniform(0, gap_s)
    while t < end_s:
        out.append((t, source, round(base_db + rng.uniform(-5, 5))))
        t += gap_s * rng.uniform(0.5, 1.5)
    return out


# 1. Clamps: garbage falls back, K never exceeds the pool, margin stays in
#    range. (Unit-level guard for the option plumbing.)
assert clamp_samples_n("nope") == DEFAULT_SAMPLES_N
assert clamp_samples_n(0) == N_MIN
assert clamp_top_k(9999, 45) == 45, "K must cap at the pool size"
assert clamp_top_k("nope", 10) == 10, "bad K falls back, then caps at the pool"
assert clamp_margin(-5) == M_MIN
assert clamp_margin(999) == M_MAX
print("PASS clamps: bad options fall back, K caps at N, margin bounded")

# 2. The field complaint, recreated: sitting 12 ft from proxy A with proxies
#    in both neighbouring rooms. Sparse capture (~1 per proxy per 10-15 s),
#    ±5 dB multipath noise, 30 minutes. The ring must not move: exactly one
#    handover (the initial pick) and zero flip-flops.
rng = random.Random(42)
feed = []
for src, base, gap in (("A", -62, 10.0), ("B", -74, 12.0), ("C", -78, 15.0)):
    feed += captures(rng, src, base, gap, 1800.0)
_, c = run("stationary three rooms", feed, "A")
t_settle = settle(c, "A")
assert t_settle < 300.0, f"took {t_settle:.0f}s to settle, expected minutes"

# 3. Lucky spike cannot flip: A is credited, B fires one anomalously strong
#    sample amid otherwise distant readings. One slot out of K is not a win.
rng = random.Random(7)
feed = []
for src, base, gap in (("A", -62, 10.0), ("B", -78, 10.0)):
    feed += captures(rng, src, base, gap, 1800.0)
feed.append((900.0, "B", -41))  # single impossible spike
feed.append((901.0, "B", -40))  # and a second, just to be sure
_, c = run("lucky spike ignored", feed, "A")
t_settle = settle(c, "A")
assert t_settle < 900.0, "the spike (at t=900) moved the credit"

# 4. Capture rate must not decide the room: B's WiFi duty cycle lets it hear
#    three times as often as A, but B hears the ring 12 dB weaker (it is in
#    the next room). RSSI-primary voting keeps the ring in A's room.
rng = random.Random(13)
feed = captures(rng, "A", -63, 9.0, 1800.0) + captures(rng, "B", -75, 3.0, 1800.0)
_, c = run("capture-rate confound", feed, "A")
t_settle = settle(c, "A")
assert t_settle < 300.0, f"count pressure kept moving the credit: {c.flips}"

# 5. Walk to the other room: A turns distant (-85) and B becomes the near
#    proxy (-56). Expect exactly one handover, after the stale strong A
#    samples age out of the pool, and no flapping afterwards.
rng = random.Random(99)
feed = []
for src, gap in (("A", 10.0), ("B", 12.0)):
    for t, s, _rssi in captures(rng, src, 0.0, gap, 1800.0):
        base = (-60, -80) if src == "A" else (-80, -56)
        feed.append((t, s, round(base[0 if t < 600 else 1] + rng.uniform(-3, 3))))
_, c = run("walk to other room", feed, "B")
t_settle = settle(c, "B")
assert 600.0 < t_settle < 1500.0, f"walk handover at t={t_settle:.0f}s, flips={c.flips}"

# 6. 127 sentinel / level-less sightings: they join the pool and can vote,
#    but never rank among the strongest; A still holds the credit it earned
#    and nothing crashes on unusable values.
feed = []
for i in range(180):  # 30 min, every 10 s
    t = i * 10.0
    feed.append((t, "A", 127))  # ESP32 "no measurement" sentinel
    feed.append((t + 1.0, "A", -55))
    feed.append((t + 2.0, "B", -55))
_, c = run("sentinel filtered", feed, "A")
settle(c, "A")

# 7. Out of range entirely: once every sample ages past the limit the pool
#    is empty and the credit (and score) are simply retained - presence is
#    the Last Seen sensor's job.
c = Credit()
for i in range(60):
    c.note("A", -50, i * 1.0)
before, rssi_before = c.source, c.rssi
assert c._credit(400.0) == (before, rssi_before), "credit lost when pool aged out"
assert not c.pool, "aged-out pool was not pruned"
print(f"PASS retained when gone: credit stays {before!r} ({rssi_before} dBm)")

# 8. Reported level: the most recent usable RSSI the winner heard - not the
#    strongest, not the median. A goes quiet-ish, its newest reading wins.
c = Credit()
for i in range(30):  # seed A as the clear winner, last sample -70
    c.note("A", -50 if i < 29 else -70, i * 1.0)
    c.note("B", -85, i * 1.0 + 0.5)
assert c.source == "A"
c.note("A", -66, 31.0)  # newest, and weaker than everything above it
assert c.rssi == -66, f"score must follow the newest reading, got {c.rssi}"
c.note("A", None, 32.0)  # a level-less sighting must not clear the score
assert c.rssi == -66, f"score lost to a level-less sighting: {c.rssi}"
print("PASS reported level: winner's most recent RSSI")

# 9. Tight pool with K > N: clamping must still resolve a dominant B cleanly.
rng = random.Random(5)
feed = captures(rng, "A", -80, 2.0, 300.0) + captures(rng, "B", -55, 2.0, 300.0)
run("K clamped to tiny pool", feed, "B", Credit(samples_n=10, top_k=100, vote_margin=0))
assert clamp_top_k(100, clamp_samples_n(10)) == 10

print("\nall proximity-credit sim cases passed")
