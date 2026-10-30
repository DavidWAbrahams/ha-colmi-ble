"""Gesture detection over the ring's RAW accelerometer stream.

Pure logic: no Home Assistant, no Bluetooth, no I/O, so it can be replayed
offline against a captured sample log.

Design notes
------------
The ring's axis mapping and sign convention on our firmware are not confirmed,
and the ring is worn on a finger whose orientation changes constantly, so the
detector never reasons about "the vertical axis". Instead it keeps a slow
exponential average of the raw vector, which *is* the gravity direction as seen
by the sensor (an accelerometer at rest reads +1 g along its own up axis).
Projecting the gravity-removed acceleration onto that direction yields a
signed *vertical* acceleration whose sign is meaningful in any wrist
orientation: positive means "accelerating the way gravity says up is", i.e.
upward. A rapid hand raise therefore produces a positive velocity bump and a
lower a negative one, regardless of which of X/Y/Z the ring happens to use.

Two guards keep ordinary life from producing gestures:

* the vertical velocity is a *leaky* integral, so a slow, deliberate arm lift
  never accumulates enough to trip it, and
* a raise/lower must also be corroborated by a real swing of the gravity
  direction between the start and the peak of the movement, so vibration or a
  jiggling walk cannot pass.

The file also holds :class:`TiltTracker`, which reports how far the ring has
tilted from the pose it was zeroed in. It is here rather than in its own module
because it reuses the same helpers and the same "feed it every sample" contract
as the detector, and because it is just as replayable offline.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .colmi_codec.motion import AccelSample
from .const import (
    MOTION_CLAP_MAX_WIDTH_S,
    MOTION_CLAP_QUIET_BAND_G,
    MOTION_CLAP_QUIET_S,
    MOTION_CLAP_REFRACTORY_S,
    MOTION_CLAP_RING_DOWN_FRACTION,
    MOTION_CLAP_RING_DOWN_S,
    MOTION_CLAP_RISE_S,
    MOTION_CLAP_SETTLE_S,
    MOTION_CLAP_SPIKE_G,
    MOTION_GESTURE_COOLDOWN_S,
    MOTION_GESTURE_SUPPRESS_S,
    MOTION_GRAVITY_FAST_TAU_S,
    MOTION_GRAVITY_TAU_S,
    MOTION_GRAVITY_TRUST_G,
    MOTION_MIN_HZ_FOR_STREAM_CLAP,
    MOTION_PITCH_RECENT_WINDOW_S,
    MOTION_RAISE_MAX_PEAK_S,
    MOTION_RAISE_PITCH_DEG,
    MOTION_RAISE_VELOCITY_G_S,
    MOTION_TILT_DEADBAND_DEG,
    MOTION_TILT_MIN_ZERO_SAMPLES,
    MOTION_TILT_TAU_S,
    MOTION_TILT_TRUST_G,
    MOTION_TILT_ZERO_STILL_S,
    MOTION_TILT_ZERO_TIMEOUT_S,
    MOTION_TILT_ZERO_TOLERANCE_DEG,
    MOTION_VELOCITY_LEAK_TAU_S,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# Gesture identifiers, used as the event entity key and in event data.
GESTURE_CLAP = "clap"
GESTURE_RAISE = "hand_raise"
GESTURE_LOWER = "hand_lower"

# Where a gesture came from, reported in event data.
SOURCE_RING = "ring"  # detected by the ring itself (camera-button gesture)
SOURCE_STREAM = "stream"  # derived here from the RAW accelerometer stream

# Samples older than this are dropped from the sliding windows.
_BUFFER_S = 2.5

# The detector must see the velocity decay to this fraction of the threshold
# before it will arm again. Without it, the braking at the top of a raise reads
# as a lower.
_REARM_FRACTION = 0.3

# An impulse counts as over once the magnitude falls back to this fraction of
# the clap threshold. Used both to time how fast a spike rises and to decide
# when an impulse above the threshold has ended.
_CLAP_LOW_FRACTION = 0.4

# Below this gravity magnitude the vector is too noisy to define "up".
_MIN_GRAVITY_G = 0.35


@dataclass(frozen=True, slots=True)
class GestureEvent:
    """A detected gesture, ready to be published to Home Assistant."""

    gesture: str
    source: str
    peak_g: float
    velocity_g_s: float

    def as_event_data(self) -> dict[str, float | str]:
        """The payload both the bus event and the entity event carry."""
        return {
            "gesture": self.gesture,
            "source": self.source,
            "peak_g": self.peak_g,
            "velocity_g_s": self.velocity_g_s,
        }


@dataclass(slots=True)
class _Movement:
    """A vertical movement in progress, plus the heading it started from."""

    sign: int
    peak: float
    start_direction: AccelSample | None
    start_time: float
    peak_time: float


def _is_still(magnitude_g: float) -> bool:
    """True if a raw magnitude says the ring is not accelerating.

    Judged on the magnitude rather than the gravity-projected acceleration on
    purpose: gravity is only an estimate, and twice per cycle a vibration's
    projected acceleration dips back through zero while its magnitude says
    undeniably "this hand is moving".
    """
    return abs(magnitude_g - 1.0) <= MOTION_CLAP_QUIET_BAND_G


def _unit(vector: AccelSample) -> AccelSample | None:
    magnitude = vector.magnitude_g
    if magnitude < _MIN_GRAVITY_G:
        return None
    return AccelSample(vector.x / magnitude, vector.y / magnitude, vector.z / magnitude)


def _angle_deg(a: AccelSample, b: AccelSample) -> float:
    """Angle in degrees between two directions (order-insensitive)."""
    cosine = max(-1.0, min(1.0, abs(a.dot(b))))
    return math.degrees(math.acos(cosine))


class GestureDetector:
    """Turn a stream of :class:`AccelSample` into :class:`GestureEvent`."""

    def __init__(self, sensitivity: float = 1.0, stream_clap: bool = True) -> None:
        """Create a detector.

        ``sensitivity`` divides the thresholds (higher = more sensitive).
        ``stream_clap`` enables the stream-derived clap; it is off unless the
        measured sample rate can actually resolve a clap impulse.
        """
        self._sensitivity = sensitivity if sensitivity > 0 else 1.0
        self.stream_clap = stream_clap

        self._gravity: AccelSample | None = None
        self._samples: deque[tuple[float, AccelSample]] = deque()
        self._velocity = 0.0
        self._dt_ema: float | None = None
        self._last_sample: float | None = None

        # Clap impulse state machine. ``_rise_start`` is the last time the
        # |linear| signal sat below the low mark, so the next crossing can be
        # timed; ``_impulse_peak`` is non-None while a candidate impulse is
        # open, i.e. between crossing the threshold and falling back down.
        self._rise_start: float | None = None
        self._impulse_peak: float | None = None
        self._impulse_start = 0.0

        # Stillness, tracked on the raw magnitude: ``_still_start`` is the start
        # of a run of non-accelerating samples still in progress, and
        # ``_quiet_len`` how long the most recent completed run lasted.
        self._still_start: float | None = None
        self._quiet_len = 0.0

        # A finished impulse is not reported until the hand has rung down
        # (see ``_resolve_pending_clap``), so it waits here with the time it
        # completed and the start of its current quiet stretch.
        self._pending: GestureEvent | None = None
        self._pending_since = 0.0
        self._ring_down_since: float | None = None

        # When the vertical velocity was last negligible, so the next movement
        # can be timed from the start of its acceleration.
        self._last_slow_time: float | None = None

        self._last_fire: dict[str, float] = {}
        self._last_any_fire: float | None = None
        self._move: _Movement | None = None

        self.last_peak_g = 0.0
        self.last_velocity_g_s = 0.0
        self.events_seen = 0
        self.samples_seen = 0

    # -- thresholds -------------------------------------------------------

    @property
    def _velocity_threshold(self) -> float:
        return MOTION_RAISE_VELOCITY_G_S / self._sensitivity

    @property
    def _clap_threshold(self) -> float:
        return MOTION_CLAP_SPIKE_G / self._sensitivity

    @property
    def _pitch_threshold_deg(self) -> float:
        # Deliberately *not* scaled by the sensitivity. The swing is not a
        # magnitude to be out-run but a qualification of the movement: easing
        # it on a sensitive setting would let a walking bounce or a vibration
        # pass as a raise, which is exactly the confusion it is here to prevent.
        return MOTION_RAISE_PITCH_DEG

    @property
    def sample_rate_hz(self) -> float | None:
        """Median-ish estimate of the incoming sample rate, from the dt EMA."""
        if self._dt_ema is None or self._dt_ema <= 0:
            return None
        return 1.0 / self._dt_ema

    def rest_magnitude_g(self) -> float | None:
        """Median raw magnitude of the buffered samples, as a decode sanity check.

        An accelerometer at rest reads about +1 g along its own up axis, so a
        median well away from that means the scale or bit-packing assumed in
        :mod:`colmi_codec.motion` is wrong rather than the gesture logic. The
        axis *order* does not affect it, which is what makes this a clean test of
        the scale alone.
        """
        if not self._samples:
            return None
        return statistics.median(
            [sample.magnitude_g for _, sample in self._samples]
        )

    @property
    def stream_clap_supported(self) -> bool:
        """True once the measured rate can resolve a clap impulse.

        Before any samples arrive this reports False; callers that want to
        decide configuration should use :meth:`rate_is_too_low` instead, which
        distinguishes "not yet known" from "measured too slow".
        """
        rate = self.sample_rate_hz
        return self.stream_clap and rate is not None and rate >= (
            MOTION_MIN_HZ_FOR_STREAM_CLAP
        )

    def rate_is_too_low(self) -> bool:
        """True when the stream is definitely too slow for a stream clap."""
        rate = self.sample_rate_hz
        return rate is not None and rate < MOTION_MIN_HZ_FOR_STREAM_CLAP

    # -- main entry point -------------------------------------------------

    def feed(
        self, sample: AccelSample, now: float | None = None
    ) -> list[GestureEvent]:
        """Feed one accelerometer sample; return any gestures it completed.

        ``now`` is a monotonic timestamp and defaults to the monotonic clock,
        which keeps offline replay honest when a capture provides its own.
        """
        if now is None:
            now = time.monotonic()

        events: list[GestureEvent] = []
        dt = 0.0
        if self._last_sample is not None:
            dt = now - self._last_sample
            # A gap (reconnect, or a ring that reports in bursts) is not a
            # measurement of the sample rate, and must not drive the
            # integrator.
            if dt <= 0 or dt > 1.0:
                dt = 0.0
            else:
                self._dt_ema = dt if self._dt_ema is None else (
                    0.2 * dt + 0.8 * self._dt_ema
                )
        self._last_sample = now

        self._samples.append((now, sample))
        self._trim(now)
        self._update_gravity(sample, dt)

        gravity = self._gravity
        if gravity is None:
            return events

        linear = sample.minus(gravity)
        peak = linear.magnitude_g
        # A decaying maximum, so the peak reported with an event and exposed for
        # tuning means "how hard was the recent movement", not "how hard has it
        # ever been since the switch was turned on".
        if dt > 0:
            self.last_peak_g = max(peak, self.last_peak_g * math.exp(-dt / _BUFFER_S))
        else:
            self.last_peak_g = max(peak, self.last_peak_g)

        up = _unit(gravity)
        if up is None:
            return events

        vertical = linear.dot(up)
        self._velocity = self._integrate(vertical, dt)
        self.last_velocity_g_s = self._velocity

        self._track_still(_is_still(sample.magnitude_g), now)
        if self.stream_clap_supported:
            settled = self._resolve_pending_clap(peak, now)
            if settled is not None:
                events.append(settled)
            candidate = self._check_clap(peak, now)
            if candidate is not None:
                self._pending = candidate
                self._pending_since = now
                self._ring_down_since = None

        events.extend(self._check_vertical(now))

        self.events_seen += len(events)
        self.samples_seen += 1
        return events

    def on_device_gesture(self, now: float | None = None) -> GestureEvent:
        """Report a gesture the ring detected itself (a clap).

        The on-device detector owns its own debouncing, and it is the preferred
        clap source because it needs no continuous connection, so this bypasses
        our thresholds; it does still respect the shared cooldown so one clap
        cannot produce both a ring and a stream event.
        """
        if now is None:
            now = time.monotonic()
        self.last_peak_g = max(self.last_peak_g, self._clap_threshold)
        event = GestureEvent(
            gesture=GESTURE_CLAP,
            source=SOURCE_RING,
            peak_g=self.last_peak_g,
            velocity_g_s=0.0,
        )
        self._record_fire(GESTURE_CLAP, now)
        self.events_seen += 1
        return event

    def reset(self) -> None:
        """Forget everything (call when a stream session restarts)."""
        self._gravity = None
        self._samples.clear()
        self._velocity = 0.0
        self._dt_ema = None
        self._last_sample = None
        self._rise_start = None
        self._impulse_peak = None
        self._impulse_start = 0.0
        self._still_start = None
        self._quiet_len = 0.0
        self._pending = None
        self._pending_since = 0.0
        self._ring_down_since = None
        self._last_slow_time = None
        self._last_fire.clear()
        self._last_any_fire = None
        self._move = None
        self.samples_seen = 0

    # -- internals --------------------------------------------------------

    def _trim(self, now: float) -> None:
        cutoff = now - _BUFFER_S
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _update_gravity(self, sample: AccelSample, dt: float) -> None:
        """Complementary filter of the *raw* vector = gravity direction.

        Normally a slow average, so a gesture cannot tilt the reference frame.
        But a sample whose magnitude is about 1 g is by definition *not*
        accelerating, which makes the raw vector a trustworthy gravity reading;
        following it quickly is what stops a changed wrist pose from being
        misread as a long, slow acceleration.
        """
        if self._gravity is None or dt <= 0:
            self._gravity = sample
            return
        tau = (
            MOTION_GRAVITY_FAST_TAU_S
            if abs(sample.magnitude_g - 1.0) <= MOTION_GRAVITY_TRUST_G
            else MOTION_GRAVITY_TAU_S
        )
        alpha = 1.0 - math.exp(-dt / tau)
        gravity = self._gravity
        self._gravity = AccelSample(
            gravity.x + alpha * (sample.x - gravity.x),
            gravity.y + alpha * (sample.y - gravity.y),
            gravity.z + alpha * (sample.z - gravity.z),
        )

    def _integrate(self, vertical: float, dt: float) -> float:
        """Leaky integral of vertical acceleration: a velocity proxy.

        The leak (``MOTION_VELOCITY_LEAK_TAU_S``) is what makes the detector
        respond to *snap* rather than displacement: a movement that takes a
        second to complete leaks away as fast as it accumulates.
        """
        if dt <= 0:
            return self._velocity
        decayed = self._velocity * math.exp(-dt / MOTION_VELOCITY_LEAK_TAU_S)
        return decayed + vertical * dt

    def _check_clap(self, peak: float, now: float) -> GestureEvent | None:
        """Short, sharp, self-limiting spike: the stream-derived clap.

        A peak threshold alone cannot tell a clap from a forceful arm
        movement, so the impulse is judged by its shape:

        * it must reach the threshold from well below within
          ``MOTION_CLAP_RISE_S``,
        * it must fall back down again within ``MOTION_CLAP_MAX_WIDTH_S``, and
        * the hand must have been *still* immediately before it.

        A clap impulse lasts tens of milliseconds; a clench or a swung arm
        holds the acceleration high for hundreds, so the gesture is reported on
        the falling edge, a sample or two after the clap itself. The stillness
        requirement is the only test that rejects a vibration: each half-cycle
        of a shake rises and falls fast enough to look like an impulse, so
        without it a shaking hand reports a machine-gun of claps.
        """
        if self._impulse_peak is None:
            self._open_impulse(peak, now)
            return None
        return self._finish_impulse(peak, now)

    def _track_still(self, still: bool, now: float) -> None:
        """Time runs of non-accelerating samples, for the clap's stillness test.

        ``_quiet_len`` is the length of the still run that ended on this sample,
        and is zeroed on every later one: only stillness *immediately* before a
        spike counts as stillness. Carrying an old run along would let the last
        half-cycle of a vibration pass for a clap, since nothing interrupts a
        sustained shake long enough to start a new run.
        """
        if still:
            if self._still_start is None:
                self._still_start = now
            return
        if self._still_start is not None:
            self._quiet_len = now - self._still_start
            self._still_start = None
        else:
            self._quiet_len = 0.0

    def _open_impulse(self, peak: float, now: float) -> None:
        """Start tracking an impulse once a still hand accelerates sharply."""
        threshold = self._clap_threshold
        if peak < threshold:
            if peak < threshold * _CLAP_LOW_FRACTION:
                # Comfortably low: a rise from here can be timed.
                self._rise_start = now
            return

        if self._quiet_len < MOTION_CLAP_QUIET_S:
            # The hand was already moving. Without this test a vibration reads
            # as a machine-gun of well-shaped impulses, because each half-cycle
            # rises fast and falls fast; a clap is preceded by a still hand.
            self._rise_start = None
            return
        if self._rise_start is None or (now - self._rise_start) > MOTION_CLAP_RISE_S:
            # Rose slowly, or we joined mid-rise: not an impulse. Stays closed
            # until the signal drops below the low mark again.
            self._rise_start = None
            return

        self._impulse_peak = peak
        self._impulse_start = now

    def _finish_impulse(self, peak: float, now: float) -> GestureEvent | None:
        """Return the impulse as a candidate once it falls back down."""
        self._impulse_peak = max(self._impulse_peak, peak)
        if peak >= self._clap_threshold * _CLAP_LOW_FRACTION:
            if now - self._impulse_start > MOTION_CLAP_MAX_WIDTH_S:
                self._close_impulse()  # held too long to be a clap
            return None

        peak_g = self._impulse_peak or 0.0
        width = now - self._impulse_start
        self._close_impulse()
        self._rise_start = now
        if width > MOTION_CLAP_MAX_WIDTH_S:
            return None
        return GestureEvent(
            gesture=GESTURE_CLAP,
            source=SOURCE_STREAM,
            peak_g=round(peak_g, 3),
            velocity_g_s=0.0,
        )

    def _resolve_pending_clap(self, peak: float, now: float) -> GestureEvent | None:
        """Award a completed impulse only once the hand has rung down.

        A clap and the first half-cycle of a shake have near-identical shapes
        while they happen, but a clap is followed by a coasting hand (constant
        velocity is no acceleration) whereas a shake follows itself with another
        half-cycle at nearly full amplitude. So the residual must fall to
        ``MOTION_CLAP_RING_DOWN_FRACTION`` of the impulse's own peak and stay
        there for ``MOTION_CLAP_SETTLE_S``; failing that within
        ``MOTION_CLAP_RING_DOWN_S`` discards the candidate. Judged relative to
        the peak because the back stroke of a real clap, the hand decelerating,
        is part of the clap. Costs ``MOTION_CLAP_SETTLE_S`` of latency.
        """
        event = self._pending
        if event is None:
            return None

        if peak > event.peak_g * MOTION_CLAP_RING_DOWN_FRACTION:
            self._ring_down_since = None
            if now - self._pending_since > MOTION_CLAP_RING_DOWN_S:
                self._pending = None
            return None
        if self._ring_down_since is None:
            self._ring_down_since = now
            return None
        if now - self._ring_down_since < MOTION_CLAP_SETTLE_S:
            return None

        self._pending = None
        self._ring_down_since = None
        if not self._can_fire(event.gesture, now, MOTION_CLAP_REFRACTORY_S):
            return None
        self._record_fire(event.gesture, now)
        return event

    def _close_impulse(self) -> None:
        """Forget an impulse that turned out to be too wide to be a clap."""
        self._impulse_peak = None
        self._rise_start = None

    def _check_vertical(self, now: float) -> list[GestureEvent]:
        """Raise/lower from the vertical velocity, corroborated by the swing.

        A movement opens when the vertical velocity crosses the threshold and
        is judged when it closes, i.e. once the velocity has decayed back
        through the re-arm mark and the hand has visibly stopped. Judging on
        the close is what makes the corroboration meaningful: it compares the
        heading the movement *started* from with the heading it ended at, which
        a vibration, a vertical bounce, or a brisk walk all fail while a real
        arm lift passes easily.
        """
        threshold = self._velocity_threshold
        if abs(self._velocity) <= threshold * _REARM_FRACTION:
            # Not moving vertically: remember when, so the next movement can be
            # timed from the start of its acceleration rather than from the
            # moment it happened to cross the firing threshold.
            self._last_slow_time = now

        move = self._move

        if move is None:
            if abs(self._velocity) < threshold:
                return []
            self._move = _Movement(
                sign=1 if self._velocity > 0 else -1,
                peak=abs(self._velocity),
                start_direction=self._current_direction(),
                start_time=self._last_slow_time if self._last_slow_time else now,
                peak_time=now,
            )
            return []

        if self._velocity * move.sign > move.peak:
            move.peak = self._velocity * move.sign
            move.peak_time = now
        if abs(self._velocity) > threshold * _REARM_FRACTION:
            return []  # still moving

        self._move = None
        gesture = GESTURE_RAISE if move.sign > 0 else GESTURE_LOWER
        if not self._movement_qualified(move, gesture, now):
            return []

        self._record_fire(gesture, now)
        return [
            GestureEvent(
                gesture=gesture,
                source=SOURCE_STREAM,
                peak_g=round(self.last_peak_g, 3),
                velocity_g_s=round(move.peak * move.sign, 4),
            )
        ]

    def _movement_qualified(self, move: _Movement, gesture: str, now: float) -> bool:
        """Whether a closed movement was really the rapid gesture it resembles."""
        if move.peak_time - move.start_time > MOTION_RAISE_MAX_PEAK_S:
            # The velocity built up gradually rather than snapping, so this was
            # a shove or a slow lift. On a sensitive setting a steady push
            # eventually accumulates enough leaked velocity to cross the
            # threshold, which is why magnitude alone is not enough.
            return False
        if not self._can_fire(gesture, now, MOTION_GESTURE_COOLDOWN_S):
            return False
        return self._swung_during(move)

    def _current_direction(self) -> AccelSample | None:
        """Heading over the most recent short window, from the raw samples.

        Both sides of the swing comparison use this same measure. That matters:
        the gravity estimate is deliberately slow, so comparing it against a
        fresh reading would report a swing whenever the wrist has *already*
        settled into a new heading, with no movement involved.
        """
        if len(self._samples) < 2:
            return None
        return self._mean_direction(
            self._samples[-1][0] - MOTION_PITCH_RECENT_WINDOW_S,
            self._samples[-1][0],
        )

    def _swung_during(self, move: _Movement) -> bool:
        """True if the ring ended ``move`` pointing somewhere it did not start."""
        ended_at = self._current_direction()
        if move.start_direction is None or ended_at is None:
            # Too few samples to compare headings. Requiring the corroborating
            # swing is the safer default: without it we would fire on anything
            # that moved.
            return False
        return _angle_deg(ended_at, move.start_direction) >= self._pitch_threshold_deg

    def _mean_direction(
        self, start: float, end: float
    ) -> AccelSample | None:
        """Unit vector of the average raw sample within ``[start, end]``."""
        total = [0.0, 0.0, 0.0]
        count = 0
        window: Iterable[tuple[float, AccelSample]] = self._samples
        for stamp, sample in window:
            if start <= stamp <= end:
                total[0] += sample.x
                total[1] += sample.y
                total[2] += sample.z
                count += 1
        if count < 2:
            return None
        return _unit(AccelSample(total[0], total[1], total[2]))

    def _can_fire(self, gesture: str, now: float, cooldown: float) -> bool:
        """Per-gesture cooldown plus a short window of mutual suppression."""
        previous = self._last_fire.get(gesture)
        if previous is not None and now - previous < cooldown:
            return False
        if (
            self._last_any_fire is not None
            and now - self._last_any_fire < MOTION_GESTURE_SUPPRESS_S
        ):
            return False
        return True

    def _record_fire(self, gesture: str, now: float) -> None:
        """Note that ``gesture`` fired, for cooldown and mutual suppression."""
        self._last_fire[gesture] = now
        self._last_any_fire = now


def _separation_deg(a: AccelSample, b: AccelSample) -> float:
    """Angle in degrees between two directions, over the full 0..180 range.

    Unlike :func:`_angle_deg` this does not ignore which way round the vectors
    are: for a tilt, pointing the ring the other way round *is* a big change in
    pose, and folding it into 0..90 would report it as a small one.
    """
    cosine = max(-1.0, min(1.0, a.dot(b)))
    return math.degrees(math.acos(cosine))


def _slerp(old: AccelSample, new: AccelSample, weight: float) -> AccelSample:
    """Rotate ``old`` toward ``new`` by ``weight`` of the angle between them.

    A plain vector blend will not do for a direction. The two opposite poses a
    hand has are *opposite vectors*, and their weighted sum is just the longer
    one, so an estimate would sit still and shrink while the hand turned around
    beneath it. Rotating through the angle instead puts the smoothing on the
    angle itself, which is the thing being measured here.
    """
    cosine = max(-1.0, min(1.0, old.dot(new)))
    angle = math.acos(cosine)
    span = math.sin(angle)
    if span < 1e-9:
        # Either already there, or exactly the other way round - which has no
        # short way round, so there is nothing to interpolate through.
        return new if angle > math.pi / 2 else old
    keep = math.sin((1.0 - weight) * angle) / span
    bring = math.sin(weight * angle) / span
    return AccelSample(
        old.x * keep + new.x * bring,
        old.y * keep + new.y * bring,
        old.z * keep + new.z * bring,
    )


class TiltTracker:
    """How far the ring has tilted from the pose it was last zeroed in.

    A wrist-worn accelerometer at rest reads gravity, so its vector says which
    way up the ring is - and how far that has changed since it was zeroed says
    how far the hand has tilted. Two caveats are inherent to the sensor rather
    than to this code, and both are why the value is a single non-negative
    angle:

    * a still accelerometer cannot see rotation *about* the gravity direction,
      so the angle is how far the ring has tilted (0..180) rather than how many
      degrees it has been rolled;
    * nothing is integrated, so the reading cannot drift. It is always relative
      to the zero pose, and always recovers it when the hand returns there.

    The precision comes from the filtering: the angle is taken between a frozen
    zero pose and a low-passed current pose, both medians or averages of unit
    vectors, because the raw signal quantizes at about 0.11 degrees at 1 g and a
    resting hand would otherwise produce several degrees of wander.
    """

    def __init__(self) -> None:
        """Start unzeroed, with no pose estimate."""
        # Low-passed direction of gravity in the ring's frame.
        self._pose: AccelSample | None = None
        # The frozen pose reported as zero degrees.
        self._zero: AccelSample | None = None
        self._prev_time: float | None = None
        self._first_time: float | None = None
        # Poses from the current run of still samples, awaiting the zero.
        self._still_start: float | None = None
        self._still: list[AccelSample] = []
        self.angle_deg: float | None = None

    @property
    def calibrated(self) -> bool:
        """True once a zero pose exists, so :attr:`angle_deg` means anything."""
        return self._zero is not None

    def feed(self, sample: AccelSample, now: float | None = None) -> float | None:
        """Feed one sample; return the current angle, or None while unzeroed.

        Every sample of the session should be fed, whether or not its reading
        gets published: the filter and the stillness window both need the
        uninterrupted stream. ``now`` is a monotonic timestamp and defaults to
        the monotonic clock, so a capture can be replayed with its own times.
        """
        if now is None:
            now = time.monotonic()

        dt = self._elapsed(now)
        unit = _unit(sample)
        if unit is None:
            return self.angle_deg

        if not self._trusts(sample.magnitude_g):
            # Moving: this direction says more about the movement than about
            # gravity. Hold the estimate rather than follow it, and treat the
            # hand as not still for the purposes of capturing a zero pose.
            self._break_stillness()
            return self.angle_deg

        self._update_pose(unit, dt)
        self._capture_zero(now)

        if self._zero is None or self._pose is None:
            return self.angle_deg

        angle = _separation_deg(self._zero, self._pose)
        self.angle_deg = 0.0 if angle < MOTION_TILT_DEADBAND_DEG else angle
        return self.angle_deg

    def zero(self) -> bool:
        """Re-zero on the pose the ring is in right now.

        This is what the zero button does. It needs nothing from the ring,
        so it works mid-session: the angle simply restarts from wherever the
        hand happens to be. Returns False (and changes nothing) while there is
        no pose estimate to zero on, which is only before the first usable
        sample.
        """
        if self._pose is None:
            return False
        self._zero = self._pose
        self._break_stillness()
        self.angle_deg = 0.0
        return True

    def reset(self) -> None:
        """Forget everything (call when a stream session starts)."""
        self._pose = None
        self._zero = None
        self._prev_time = None
        self._first_time = None
        self._break_stillness()
        self.angle_deg = None

    # -- internals --------------------------------------------------------

    def _elapsed(self, now: float) -> float:
        """Seconds since the previous sample; 0 for the first, or after a gap.

        A gap (a reconnect, or a ring that reports in bursts) is not a
        measurement of elapsed pose change, so the filter is held instead of
        jumping.
        """
        if self._first_time is None:
            self._first_time = now
        dt = 0.0
        if self._prev_time is not None:
            dt = now - self._prev_time
            if dt <= 0 or dt > 1.0:
                dt = 0.0
                self._break_stillness()
        self._prev_time = now
        return dt

    @staticmethod
    def _trusts(magnitude_g: float) -> bool:
        """True if a magnitude near 1 g means gravity is what we are reading."""
        return abs(magnitude_g - 1.0) <= MOTION_TILT_TRUST_G

    def _update_pose(self, unit: AccelSample, dt: float) -> None:
        if self._pose is None:
            self._pose = unit
            return
        if dt <= 0:
            return
        alpha = 1.0 - math.exp(-dt / MOTION_TILT_TAU_S)
        self._pose = _slerp(self._pose, unit, alpha)

    def _capture_zero(self, now: float) -> None:
        """Zero on the first pose the hand rests still in.

        Only the *first* zero is captured like this. Afterwards it stays frozen,
        so the reading can never creep back toward zero by itself, and the only
        thing that moves it is :meth:`zero`.
        """
        if self._zero is not None or self._pose is None:
            return

        if self._still_start is None or self._strays(self._pose):
            self._still_start = now
            self._still = [self._pose]
        else:
            self._still.append(self._pose)

        rested = now - self._still_start >= MOTION_TILT_ZERO_STILL_S
        rested = rested and len(self._still) >= MOTION_TILT_MIN_ZERO_SAMPLES
        waiting = self._first_time if self._first_time is not None else now
        if rested or now - waiting >= MOTION_TILT_ZERO_TIMEOUT_S:
            self._set_zero(self._median_pose())

    def _strays(self, pose: AccelSample) -> bool:
        """True if ``pose`` no longer agrees with the poses gathered so far.

        Sitting near 1 g is not the same as being still: a slow rotation reads as
        1 g the whole way round. So the window must also be a run of poses that
        agree with each other, and a pose pointing appreciably away from the
        median of the window so far starts a new window rather than widening the
        current one.
        """
        anchor = self._window_median()
        if anchor is None:
            return True
        return _separation_deg(anchor, pose) > MOTION_TILT_ZERO_TOLERANCE_DEG

    def _set_zero(self, pose: AccelSample | None) -> None:
        if pose is None:
            return
        self._zero = pose
        self.angle_deg = 0.0
        self._break_stillness()

    def _median_pose(self) -> AccelSample | None:
        """The window's pose, or the filtered estimate if the window disagrees.

        Directions that disagree wildly can average out to something short, even
        near zero, and then they define no pose at all; the estimate is the next
        best thing, since at least it tracks the stream.
        """
        return self._window_median() or self._pose

    def _window_median(self) -> AccelSample | None:
        """Per-axis median of the gathered poses, as a direction, if usable."""
        if not self._still:
            return None
        return _unit(
            AccelSample(
                statistics.median(s.x for s in self._still),
                statistics.median(s.y for s in self._still),
                statistics.median(s.z for s in self._still),
            )
        )

    def _break_stillness(self) -> None:
        self._still_start = None
        self._still.clear()

