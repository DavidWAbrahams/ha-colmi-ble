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

The file also holds :class:`VerticalAngleTracker`, which reports how far the
ring's hole axis is deflected from the horizontal plane, measured absolutely
against gravity. It is here rather than in its own module because it reuses the
same helpers and the same "feed it every sample" contract as the detector, and
because it is just as replayable offline.
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
    MOTION_GESTURE_COOLDOWN_S,
    MOTION_GESTURE_SUPPRESS_S,
    MOTION_GRAVITY_FAST_TAU_S,
    MOTION_GRAVITY_TAU_S,
    MOTION_GRAVITY_TRUST_G,
    MOTION_PITCH_RECENT_WINDOW_S,
    MOTION_RAISE_MAX_PEAK_S,
    MOTION_RAISE_PITCH_DEG,
    MOTION_RAISE_VELOCITY_G_S,
    MOTION_VELOCITY_LEAK_TAU_S,
    MOTION_VERTICAL_AXIS,
    MOTION_VERTICAL_DEADBAND_DEG,
    MOTION_VERTICAL_SNAP_DEG,
    MOTION_VERTICAL_SNAP_TAU_S,
    MOTION_VERTICAL_TAU_S,
    MOTION_VERTICAL_TRUST_G,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# Gesture identifiers, used as the event entity key and in event data.
GESTURE_WAVE = "wave"
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

    def __init__(self, sensitivity: float = 1.0) -> None:
        """Create a detector.

        ``sensitivity`` divides the thresholds (higher = more sensitive).
        """
        self._sensitivity = sensitivity if sensitivity > 0 else 1.0

        self._gravity: AccelSample | None = None
        self._samples: deque[tuple[float, AccelSample]] = deque()
        self._velocity = 0.0
        self._dt_ema: float | None = None
        self._last_sample: float | None = None

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

        events.extend(self._check_vertical(now))

        self.events_seen += len(events)
        self.samples_seen += 1
        return events

    def reset(self) -> None:
        """Forget everything (call when a stream session restarts)."""
        self._gravity = None
        self._samples.clear()
        self._velocity = 0.0
        self._dt_ema = None
        self._last_sample = None
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


class VerticalAngleTracker:
    """How far the ring's hole axis is deflected from the horizontal plane.

    A ring at rest reads gravity, so the sensor's own frame says where the hole
    axis (see ``MOTION_VERTICAL_AXIS``) points relative to "down". Per sample
    the elevation of that axis above the horizontal plane is ``asin(|axis|)``
    of the unit gravity direction, and the reported angle is that elevation
    low-passed:

    * ``0`` when the hole axis is horizontal - little or no gravity along it;
    * ``90`` when it points at the sky or the floor - gravity of about 1 g
      along it, either sign.

    Three properties fall out of the formula, and all three are wanted here:

    * it is absolute: nothing is zeroed against a starting pose, nothing is
      integrated, so the reading cannot drift and needs no calibration;
    * it is folded into 0..90 by the absolute value, so turning the ring
      end-for-end (which flips the sign of the axis) changes nothing;
    * it ignores rotation in the horizontal plane (yaw): such a rotation moves
      only the *other* two axes, so the per-sample elevation does not move,
      and neither does the smoothed value. This is also why the smoothing is
      an average of the *angle* rather than of the gravity vector: the great
      circle between two consecutive yawed vectors bulges toward the pole, so
      a vector-space filter would read a fast spin as a rise in elevation.

    The precision comes from the filtering: the raw signal quantizes at about
    0.11 degrees at 1 g and ``asin`` amplifies that near vertical, so a
    resting hand would otherwise produce degrees of wander. The filter is
    adaptive, though: while the raw target sits more than
    ``MOTION_VERTICAL_SNAP_DEG`` away from the estimate the smoothing uses the
    short ``MOTION_VERTICAL_SNAP_TAU_S``, so a deliberate flip of the ring
    settles in well under a second instead of visibly sliding.
    """

    def __init__(self) -> None:
        """Start with no estimate."""
        # Low-passed elevation of the hole axis, in degrees (0..90).
        self._elevation: float | None = None
        self._prev_time: float | None = None
        self.angle_deg: float | None = None

    def feed(self, sample: AccelSample, now: float | None = None) -> float | None:
        """Feed one sample; return the current angle, or None until trusted.

        Every sample of the session should be fed, whether or not its reading
        gets published: the low-pass needs the uninterrupted stream. ``now`` is
        a monotonic timestamp and defaults to the monotonic clock, so a capture
        can be replayed with its own times.
        """
        if now is None:
            now = time.monotonic()

        dt = self._elapsed(now)
        unit = _unit(sample)
        if unit is None:
            return self.angle_deg

        if not self._trusts(sample.magnitude_g):
            # Moving: this direction says more about the movement than about
            # gravity. Hold the estimate rather than follow it.
            return self.angle_deg

        # |component| of the unit gravity direction along the hole axis is
        # sin(elevation); asin recovers the elevation, folded to 0..90 because
        # the axis reading gravity at +1 g and at -1 g is equally vertical.
        along = abs(getattr(unit, MOTION_VERTICAL_AXIS))
        target = math.degrees(math.asin(min(1.0, along)))

        if self._elevation is None:
            self._elevation = target
        elif dt > 0:
            # Adaptive: follow quickly while far from the estimate (a real,
            # large movement, not noise), slowly when close (a resting hand).
            gap = abs(target - self._elevation)
            tau = (
                MOTION_VERTICAL_SNAP_TAU_S
                if gap > MOTION_VERTICAL_SNAP_DEG
                else MOTION_VERTICAL_TAU_S
            )
            alpha = 1.0 - math.exp(-dt / tau)
            self._elevation += alpha * (target - self._elevation)
        # dt <= 0 (a gap, or two stamps in one tick): hold the estimate.

        angle = self._elevation
        self.angle_deg = 0.0 if angle < MOTION_VERTICAL_DEADBAND_DEG else angle
        return self.angle_deg

    def reset(self) -> None:
        """Forget everything (call when a stream session starts)."""
        self._elevation = None
        self._prev_time = None
        self.angle_deg = None

    # -- internals --------------------------------------------------------

    def _elapsed(self, now: float) -> float:
        """Seconds since the previous sample; 0 for the first, or after a gap.

        A gap (a reconnect, or a ring that reports in bursts) is not a
        measurement of elapsed angle change, so the filter is held instead of
        jumping.
        """
        dt = 0.0
        if self._prev_time is not None:
            dt = now - self._prev_time
            if dt <= 0 or dt > 1.0:
                dt = 0.0
        self._prev_time = now
        return dt

    @staticmethod
    def _trusts(magnitude_g: float) -> bool:
        """True if a magnitude near 1 g means gravity is what we are reading."""
        return abs(magnitude_g - 1.0) <= MOTION_VERTICAL_TRUST_G

