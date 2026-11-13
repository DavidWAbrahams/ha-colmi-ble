"""
Gesture detection over the ring's RAW accelerometer stream.

Pure logic: no Home Assistant, no Bluetooth, no I/O, so it can be replayed
offline against a captured sample log.

Design notes
------------
Stock R02 firmware reports one accelerometer sample per second, and that
sample rate rules out every gesture built on sub-second transients: a leaky
velocity integrator loses most of its accumulated velocity between samples,
and a movement that takes half a second to complete is over before the ring
looks. What survives that rate are *poses* - directions the hand holds for
seconds, which a single sample per second is plenty to confirm.

:class:`StreamGestureDetector` therefore detects four pose gestures, all read
from the raw vector as gravity direction plus movement:

* ``hand_still``: the gravity-removed acceleration stays small for
  ``gesture_hold_still_s`` seconds. Fires once per quiet episode, so holding
  a resting hand produces one event rather than one per second.
* ``raised_hand``: the same calm episode, but with the hole axis also held
  high - at least ``MOTION_RAISED_HAND_MIN_DEG`` on the Vertical Angle
  sensor's scale - for ``gesture_hold_raised_s`` seconds. It is the
  stillness gesture plus an elevation gate: a raised, steady hand (the
  classic "may I ask a question?" pose) fires, while the same hand resting
  on a desk does not.
* ``hand_flip``: a held pose, a quick turn, and a second held pose far from
  the first - in practice palm up, flip, palm down. The two poses are compared
  as the *angle between their gravity directions*, close to 180 degrees for a
  palm flip, so the check never assumes which sensor axis points where. The
  comparison deliberately keeps the sign of the dot product: folding it (as
  :func:`_separation_deg` is careful *not* to do, unlike an axis-elevation
  measure) would read a 180-degree flip as no movement at all. The turn must
  also be quick, judged against a window that scales with the measured sample
  interval: a stream that reports once per second cannot observe a sub-second
  transition at all, so a few sample intervals is the most faithful reading
  of "less than a second" the firmware allows, while a faster firmware keeps
  a tight window.

* ``hand_chop``: a raised pose becomes flat. The hole axis is held above
  ``MOTION_CHOP_RAISED_MIN_DEG`` to arm the gesture, then must land below
  ``MOTION_CHOP_FLAT_MAX_DEG`` and be held there for ``gesture_hold_chop_s``
  seconds, with the drop itself judged quick by the same rate-scaled
  transition window the flip uses. The two elevation bands are deliberately
  separated by a gap, so resting at chest height neither arms the gesture
  nor completes one.

The old flick detector that this replaced integrated vertical velocity to
separate a snap raise from a slow lift; at 1 Hz it could not resolve either,
and what it fired on was a tilt, so it and its sensitivity option were removed
rather than left to misfire.

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

from .colmi_codec.motion import AccelSample
from .const import (
    DEFAULT_GESTURE_HOLD_CHOP_S,
    DEFAULT_GESTURE_HOLD_FLIP_S,
    DEFAULT_GESTURE_HOLD_RAISED_S,
    DEFAULT_GESTURE_HOLD_STILL_S,
    MOTION_CHOP_FLAT_MAX_DEG,
    MOTION_CHOP_RAISED_MIN_DEG,
    MOTION_FLIP_MIN_ANGLE_DEG,
    MOTION_FLIP_TRANSITION_BASE_S,
    MOTION_FLIP_TRANSITION_DT_FACTOR,
    MOTION_GRAVITY_FAST_TAU_S,
    MOTION_GRAVITY_TAU_S,
    MOTION_GRAVITY_TRUST_G,
    MOTION_HOLD_TOLERANCE_DEG,
    MOTION_RAISED_HAND_MIN_DEG,
    MOTION_SAMPLE_GAP_MAX_S,
    MOTION_STILL_ACCEL_G,
    MOTION_VERTICAL_AXIS,
    MOTION_VERTICAL_SNAP_DEG,
    MOTION_VERTICAL_SNAP_TAU_S,
    MOTION_VERTICAL_TAU_S,
    MOTION_VERTICAL_TRUST_G,
)

# Gesture identifiers, used as the event entity key and in event data.
GESTURE_WAVE = "wave"
GESTURE_STILL = "hand_still"
GESTURE_FLIP = "hand_flip"
GESTURE_RAISED_HAND = "raised_hand"
GESTURE_CHOP = "hand_chop"

# Where a gesture came from, reported in event data.
SOURCE_RING = "ring"  # detected by the ring itself (camera-button gesture)
SOURCE_STREAM = "stream"  # derived here from the RAW accelerometer stream

# Samples older than this are dropped from the magnitude buffer kept for the
# decode sanity check.
_BUFFER_S = 5.0

# Below this gravity magnitude the vector is too noisy to define "up".
_MIN_GRAVITY_G = 0.35


@dataclass(frozen=True, slots=True)
class GestureEvent:
    """A detected gesture, ready to be published to Home Assistant."""

    gesture: str
    source: str
    # Seconds the relevant pose (or calm) was held when the gesture fired.
    # 0 for the ring's own wave report, which is a statement, not a
    # measurement, and 0 for gestures with no hold behind them.
    hold_s: float = 0.0
    # The gesture's pose angle in degrees: the separation between the two
    # poses of a flip, or the hole-axis elevation a raised hand fired at.
    # 0 otherwise.
    angle_deg: float = 0.0

    def as_event_data(self) -> dict[str, float | str]:
        """The payload both the bus event and the entity event carry."""
        return {
            "gesture": self.gesture,
            "source": self.source,
            "hold_s": self.hold_s,
            "angle_deg": self.angle_deg,
        }


@dataclass(slots=True)
class _PoseHold:
    """
    A wrist pose, and how long the ring has been holding it.

    ``direction`` is the unit gravity vector of the sample that opened the
    hold; the hold lives while calm samples keep arriving within the tolerance
    of it. ``confirmed`` marks that it has lasted long enough to be a hold
    rather than a moment of stillness - only confirmed poses can anchor the
    flip check.
    """

    direction: AccelSample
    start_time: float
    last_calm_time: float
    confirmed: bool = False


def _unit(vector: AccelSample) -> AccelSample | None:
    magnitude = vector.magnitude_g
    if magnitude < _MIN_GRAVITY_G:
        return None
    return AccelSample(vector.x / magnitude, vector.y / magnitude, vector.z / magnitude)


def _separation_deg(a: AccelSample, b: AccelSample) -> float:
    """
    Angle in degrees between two directions, 0..180.

    No absolute value on the dot product: 180 degrees must read as 180, since
    that is exactly the number a palm flip produces. (The vertical elevation
    below wants the folded, sign-free version.)
    """
    cosine = max(-1.0, min(1.0, a.dot(b)))
    return math.degrees(math.acos(cosine))


def _elevation_deg(unit: AccelSample, hole_axis: str = MOTION_VERTICAL_AXIS) -> float:
    """
    Raw elevation of the hole axis above the horizontal plane, 0..90.

    ``|component|`` of the unit gravity direction along the hole axis is
    ``sin(elevation)``; ``asin`` recovers the elevation, folded to 0..90
    because the axis reading gravity at +1 g and at -1 g is equally vertical.

    ``hole_axis`` names the sensor axis the ring hole runs through: most
    firmware maps it onto Z, but not all (see ``const.MOTION_VERTICAL_AXIS``),
    so callers pass the per-ring answer resolved by
    ``const.resolve_hole_axis``.
    """
    along = abs(getattr(unit, hole_axis))
    return math.degrees(math.asin(min(1.0, along)))


class StreamGestureDetector:
    """
    Turn a stream of :class:`AccelSample` into :class:`GestureEvent`.

    Feed it every sample of a session and it returns the ``hand_still``,
    ``hand_flip`` and ``raised_hand`` gestures the stream completes. It also
    reports the stream's sample rate and median magnitude, which is everything
    ``MotionManager._async_check_rate`` needs to log what the stream actually
    looks like once per session.
    """

    def __init__(
        self,
        still_hold_s: float = DEFAULT_GESTURE_HOLD_STILL_S,
        flip_hold_s: float = DEFAULT_GESTURE_HOLD_FLIP_S,
        raised_hold_s: float = DEFAULT_GESTURE_HOLD_RAISED_S,
        chop_hold_s: float = DEFAULT_GESTURE_HOLD_CHOP_S,
        hole_axis: str = MOTION_VERTICAL_AXIS,
    ) -> None:
        """
        Create a detector with per-gesture hold thresholds.

        ``still_hold_s`` is how long a calm episode must last before
        ``hand_still`` fires; ``flip_hold_s`` is how long each of the two
        poses of a ``hand_flip`` must be held; ``raised_hold_s`` is how long
        the hand must stay calm *and* raised before ``raised_hand`` fires;
        ``chop_hold_s`` is how long each half of a ``hand_chop`` - the raised
        pose that arms it and the flat pose that completes it - must be held.
        ``hole_axis`` is only used on the fallback elevation path (see
        :meth:`feed`); pass what the manager resolved for this ring.
        """
        self._still_hold_s = self._clamp_hold(
            still_hold_s, DEFAULT_GESTURE_HOLD_STILL_S
        )
        self._flip_hold_s = self._clamp_hold(flip_hold_s, DEFAULT_GESTURE_HOLD_FLIP_S)
        self._raised_hold_s = self._clamp_hold(
            raised_hold_s, DEFAULT_GESTURE_HOLD_RAISED_S
        )
        self._chop_hold_s = self._clamp_hold(chop_hold_s, DEFAULT_GESTURE_HOLD_CHOP_S)
        self._hole_axis = hole_axis

        self._gravity: AccelSample | None = None
        self._samples: deque[tuple[float, AccelSample]] = deque()
        self._dt_ema: float | None = None
        self._last_sample: float | None = None

        # The pose being held right now, and the last pose that was confirmed
        # long enough to anchor a flip. The anchor keeps its last calm stamp
        # live while the same pose keeps holding, and freezes when the hand
        # moves on, which is exactly what times the flip's transition.
        self._hold: _PoseHold | None = None
        self._anchor: _PoseHold | None = None

        # Calm episode for the stillness gesture: when it started, and whether
        # it has already fired (one event per episode, not per second).
        self._still_since: float | None = None
        self._still_fired = False

        # The same for the raised-hand episode: calm with the hole axis held
        # high. Its own clock because its hold is shorter, so an episode can
        # satisfy the raised gate before (or without) the stillness one.
        self._raised_since: float | None = None
        self._raised_fired = False

        # The chop's three clocks: when the current raised (arming) episode
        # started, when the raised pose was last confirmed calm (the drop's
        # start), and when the current flat hold started. ``_chop_armed`` is
        # the raised half having lasted long enough; ``_chop_fired`` keeps one
        # event per chop.
        self._chop_raised_since: float | None = None
        self._chop_raised_last: float | None = None
        self._chop_armed = False
        self._chop_flat_since: float | None = None
        self._chop_fired = False

        self.events_seen = 0
        self.samples_seen = 0

    @staticmethod
    def _clamp_hold(value: float, fallback: float) -> float:
        """Coerce an option value into the usable 1..60 s hold range."""
        try:
            hold = float(value)
        except (TypeError, ValueError):
            hold = float(fallback)
        return min(max(hold, 1.0), 60.0)

    # -- stream diagnostics (for MotionManager's once-per-session log) ----

    @property
    def sample_rate_hz(self) -> float | None:
        """Median-ish estimate of the incoming sample rate, from the dt EMA."""
        if self._dt_ema is None or self._dt_ema <= 0:
            return None
        return 1.0 / self._dt_ema

    def rest_magnitude_g(self) -> float | None:
        """
        Median raw magnitude of the buffered samples, as a decode sanity check.

        An accelerometer at rest reads about +1 g along its own up axis, so a
        median well away from that means the scale or bit-packing assumed in
        :mod:`colmi_codec.motion` is wrong rather than the gesture logic. The
        axis *order* does not affect it, which is what makes this a clean test of
        the scale alone.
        """
        if not self._samples:
            return None
        return statistics.median([sample.magnitude_g for _, sample in self._samples])

    # -- main entry point -------------------------------------------------

    def feed(
        self,
        sample: AccelSample,
        now: float | None = None,
        angle_deg: float | None = None,
    ) -> list[GestureEvent]:
        """
        Feed one accelerometer sample; return any gestures it completed.

        ``now`` is a monotonic timestamp and defaults to the monotonic clock,
        which keeps offline replay honest when a capture provides its own.

        ``angle_deg`` is the current reading of the Vertical Angle sensor,
        when one is available: the raised-hand gate is judged against exactly
        what that sensor reports (calibrated and smoothed), so a user watching
        the sensor sees the gesture fire when the number says it should. It
        is optional so the detector stays replayable on raw captures alone;
        without it the elevation is read from the sample directly.
        """
        if now is None:
            now = time.monotonic()

        dt, gap = self._elapsed(now)
        self._samples.append((now, sample))
        self._trim(now)

        if gap:
            # A silence longer than any plausible sample interval means the
            # stream went away and came back (reconnect, dropped burst). What
            # the hand did in the silence is unknowable, so every episode open
            # across it is dropped rather than judged.
            self._hold = None
            self._anchor = None
            self._still_since = None
            self._still_fired = False
            self._raised_since = None
            self._raised_fired = False
            self._reset_chop()

        if self._gravity is None:
            self._gravity = sample
        elif dt > 0:
            # A same-tick duplicate (dt <= 0) must not move the reference or
            # count as time passing for the fast filter below.
            self._update_gravity(sample, dt)

        unit = _unit(sample)
        calm = (
            unit is not None
            and sample.minus(self._gravity).magnitude_g <= MOTION_STILL_ACCEL_G
        )
        elevation = (
            angle_deg
            if angle_deg is not None
            else (_elevation_deg(unit, self._hole_axis) if unit is not None else None)
        )

        events: list[GestureEvent] = []
        events.extend(self._track_stillness(calm=calm, now=now))
        events.extend(self._track_pose(calm=calm, unit=unit, now=now))
        events.extend(self._track_raised_hand(calm=calm, elevation=elevation, now=now))
        events.extend(self._track_chop(calm=calm, elevation=elevation, now=now))

        self.events_seen += len(events)
        self.samples_seen += 1
        return events

    def reset(self) -> None:
        """Forget everything (call when a stream session restarts)."""
        self._gravity = None
        self._samples.clear()
        self._dt_ema = None
        self._last_sample = None
        self._hold = None
        self._anchor = None
        self._still_since = None
        self._still_fired = False
        self._raised_since = None
        self._raised_fired = False
        self._reset_chop()
        self.events_seen = 0
        self.samples_seen = 0

    # -- internals --------------------------------------------------------

    def _elapsed(self, now: float) -> tuple[float, bool]:
        """
        Seconds since the previous sample, and whether the stream gapped.

        The dt EMA (and with it the sample rate and the flip's transition
        window) only ever sees plausible sample intervals: a gap is not a
        measurement of either.
        """
        if self._last_sample is None:
            self._last_sample = now
            return 0.0, False
        dt = now - self._last_sample
        self._last_sample = now
        if dt <= 0:
            return 0.0, False
        if dt > MOTION_SAMPLE_GAP_MAX_S:
            return 0.0, True
        self._dt_ema = dt if self._dt_ema is None else (0.2 * dt + 0.8 * self._dt_ema)
        return dt, False

    def _trim(self, now: float) -> None:
        cutoff = now - _BUFFER_S
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _update_gravity(self, sample: AccelSample, dt: float) -> None:
        """
        Complementary filter of the *raw* vector = gravity direction.

        Normally a slow average, so a movement cannot tilt the reference frame
        and every still sample would then be measured against a frame moving
        the way the hand just moved. But a sample whose magnitude is about 1 g
        is by definition *not* accelerating, which makes the raw vector a
        trustworthy gravity reading; following it quickly is what lets a new
        palm pose become the reference within one or two samples, which in turn
        is what makes the *next* pose's stillness measurable.
        """
        tau = (
            MOTION_GRAVITY_FAST_TAU_S
            if abs(sample.magnitude_g - 1.0) <= MOTION_GRAVITY_TRUST_G
            else MOTION_GRAVITY_TAU_S
        )
        alpha = 1.0 - math.exp(-dt / tau)
        gravity = self._gravity
        if gravity is None:  # pragma: no cover - caller guarantees the first
            self._gravity = sample
            return
        self._gravity = AccelSample(
            gravity.x + alpha * (sample.x - gravity.x),
            gravity.y + alpha * (sample.y - gravity.y),
            gravity.z + alpha * (sample.z - gravity.z),
        )

    def _track_stillness(self, *, calm: bool, now: float) -> list[GestureEvent]:
        """
        One ``hand_still`` per calm episode, once it has lasted ``still_hold_s``.

        Calm means the gravity-removed acceleration stays small: a held pose,
        however slightly unsteady, sits inside the threshold, while a walking
        or gesturing hand does not. Slowly *drifting* the hand is a separate
        question - it barely accelerates, so it reads as calm - and that is
        accepted: the gesture says "this hand is not moving about", which is
        what the acceleration actually measures.
        """
        if not calm:
            self._still_since = None
            self._still_fired = False
            return []
        if self._still_since is None:
            self._still_since = now
            return []
        held = now - self._still_since
        if self._still_fired or held < self._still_hold_s:
            return []
        self._still_fired = True
        return [
            GestureEvent(
                gesture=GESTURE_STILL,
                source=SOURCE_STREAM,
                hold_s=round(held, 2),
            )
        ]

    def _track_raised_hand(
        self, *, calm: bool, elevation: float | None, now: float
    ) -> list[GestureEvent]:
        """
        One ``raised_hand`` per raised-and-calm episode, once it lasts long enough.

        The same episode logic as :meth:`_track_stillness`, gated additionally
        on the hole axis pointing high: either the hand moving or the hand
        dropping below ``MOTION_RAISED_HAND_MIN_DEG`` ends the episode and
        re-arms the gesture. The elevation is judged per sample rather than
        averaged: a calm hand's elevation is already a settled reading (the
        sensor value itself is low-passed when supplied), and a droop below
        the threshold is a real lowering of the hand, not noise. The gesture
        deliberately overlaps ``hand_still`` - a raised hand held long enough
        fires both, and automations pick which one they mean.
        """
        if not calm or elevation is None or elevation < MOTION_RAISED_HAND_MIN_DEG:
            self._raised_since = None
            self._raised_fired = False
            return []
        if self._raised_since is None:
            self._raised_since = now
            return []
        held = now - self._raised_since
        if self._raised_fired or held < self._raised_hold_s:
            return []
        self._raised_fired = True
        return [
            GestureEvent(
                gesture=GESTURE_RAISED_HAND,
                source=SOURCE_STREAM,
                hold_s=round(held, 2),
                angle_deg=round(elevation, 1),
            )
        ]

    def _reset_chop(self) -> None:
        """Forget every clock of the hand chop gesture."""
        self._chop_raised_since = None
        self._chop_raised_last = None
        self._chop_armed = False
        self._chop_flat_since = None
        self._chop_fired = False

    def _track_chop(
        self, *, calm: bool, elevation: float | None, now: float
    ) -> list[GestureEvent]:
        """
        One ``hand_chop`` per raised-then-flat move, once both halves hold.

        The raised half arms the gesture the way ``raised_hand`` works: calm
        with the hole axis above ``MOTION_CHOP_RAISED_MIN_DEG`` for
        ``chop_hold_s`` seconds. Its clock keeps its last calm stamp live
        across brief shakes (a non-calm sample stops it progressing rather
        than ending it, exactly like the flip's anchor), and that stamp is
        what times the drop. Once armed, a calm sample below
        ``MOTION_CHOP_FLAT_MAX_DEG`` opens the flat hold; the gesture fires
        when that hold too has lasted ``chop_hold_s`` and the drop between
        the two was quick by the flip's rate-scaled window. The elevation
        bands are separated by a gap, and the drop itself is never calm, so
        no sample belongs to both halves: a slow lowering simply ages the
        transition window out and never fires. Firing disarms the gesture,
        so the hand must be raised again for the next chop.
        """
        if elevation is None:
            return []

        events: list[GestureEvent] = []
        if calm and elevation >= MOTION_CHOP_RAISED_MIN_DEG:
            # Raised half: the arming episode. (It also closes any half-open
            # flat hold - raising back up is not a completion.)
            self._chop_flat_since = None
            if self._chop_raised_since is None:
                self._chop_raised_since = now
            self._chop_raised_last = now
            if (
                not self._chop_armed
                and now - self._chop_raised_since >= self._chop_hold_s
            ):
                self._chop_armed = True
                self._chop_fired = False
        elif calm and elevation <= MOTION_CHOP_FLAT_MAX_DEG:
            # Flat half: only counts if the raised half armed it.
            self._chop_raised_since = None
            if self._chop_armed and self._chop_raised_last is not None:
                if self._chop_flat_since is None:
                    self._chop_flat_since = now
                elif not self._chop_fired:
                    held = now - self._chop_flat_since
                    transition = self._chop_flat_since - self._chop_raised_last
                    if (
                        held >= self._chop_hold_s
                        and transition <= self._transition_window_s()
                    ):
                        self._chop_fired = True
                        self._chop_armed = False
                        self._chop_raised_last = None
                        events.append(
                            GestureEvent(
                                gesture=GESTURE_CHOP,
                                source=SOURCE_STREAM,
                                hold_s=round(held, 2),
                                angle_deg=round(elevation, 1),
                            )
                        )
        # Between the bands, or a sample too rough to read gravity from: the
        # drop's transition simply keeps aging against the frozen stamps.
        return events

    def _track_pose(
        self, *, calm: bool, unit: AccelSample | None, now: float
    ) -> list[GestureEvent]:
        """
        Advance the current pose hold; confirm and maybe fire a flip.

        A non-calm sample does not end a hold, it just stops it progressing:
        what ends it is the hand settling somewhere else, because the hold and
        the flip are both about where the pose *ended up*. If the hand moves
        for long enough the transition window closes on its own, so a slow
        change of pose confirms a new hold without ever being called a flip.
        """
        if not calm or unit is None:
            return []

        hold = self._hold
        if (
            hold is not None
            and _separation_deg(hold.direction, unit) <= MOTION_HOLD_TOLERANCE_DEG
        ):
            hold.last_calm_time = now
            if hold.confirmed or now - hold.start_time < self._flip_hold_s:
                return []
            return self._confirm_hold(hold, now)

        # Settled into a pose far enough from the current hold that it is a
        # different pose: this sample opens a new hold.
        self._hold = _PoseHold(direction=unit, start_time=now, last_calm_time=now)
        return []

    def _confirm_hold(self, hold: _PoseHold, now: float) -> list[GestureEvent]:
        """
        A pose has been held long enough; compare it with the last one.

        The flip is the whole gesture the user described: hold one palm pose,
        turn, hold the opposite one. Both halves are required before anything
        fires, so the event arrives at the end of the second hold and each
        flip of a back-and-forth sequence fires in turn - the confirmed pose
        just became the anchor for the next one.
        """
        hold.confirmed = True
        anchor = self._anchor
        events: list[GestureEvent] = []
        if anchor is not None and anchor is not hold:
            separation = _separation_deg(anchor.direction, hold.direction)
            transition = hold.start_time - anchor.last_calm_time
            if (
                separation >= MOTION_FLIP_MIN_ANGLE_DEG
                and transition <= self._transition_window_s()
            ):
                events.append(
                    GestureEvent(
                        gesture=GESTURE_FLIP,
                        source=SOURCE_STREAM,
                        hold_s=round(now - hold.start_time, 2),
                        angle_deg=round(separation, 1),
                    )
                )
        self._anchor = hold
        return events

    def _transition_window_s(self) -> float:
        """
        Longest transition a flip may take, given the measured sample rate.

        The floor (``MOTION_FLIP_TRANSITION_BASE_S``) is the user's "less than
        a second" and binds on fast streams. The dt term exists because a 1 Hz
        stream cannot see a transition at all: one second between holds is one
        sample, and accepting "a few samples" is the only reading that does
        not silently disable the gesture on stock firmware.
        """
        dt = self._dt_ema if self._dt_ema is not None else 0.0
        return max(
            MOTION_FLIP_TRANSITION_BASE_S,
            MOTION_FLIP_TRANSITION_DT_FACTOR * dt,
        )


class VerticalAngleTracker:
    """
    How far the ring's hole axis is deflected from the horizontal plane.

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

    def __init__(
        self,
        flat_deg: float = 0.0,
        vertical_deg: float = 90.0,
        hole_axis: str = MOTION_VERTICAL_AXIS,
    ) -> None:
        """
        Start with no estimate, calibrated to the given anchors.

        ``flat_deg`` and ``vertical_deg`` are the *raw* elevations that mean flat
        and vertical; the reported angle rescales raw elevations linearly so
        those map to 0 and 90. The defaults are the uncorrected identity: with
        real sensor bias the raw reading tops out below 90 (see
        ``const.CONF_VERTICAL_UPRIGHT_DEG``), and that offset is a property of
        the unit and the wear, so the anchors come from the entry options.

        ``hole_axis`` names the sensor axis the ring hole runs through - Z for
        most firmware, Y on an R08 - as resolved per ring by
        ``const.resolve_hole_axis``; see :func:`_elevation_deg`.
        """
        # Low-passed elevation of the hole axis, in degrees (0..90), raw scale.
        self._elevation: float | None = None
        self._prev_time: float | None = None
        self.angle_deg: float | None = None
        # Output calibration anchors; guaranteed flat < vertical by the caller
        # (const.resolve_vertical_anchors).
        self._flat_deg = flat_deg
        self._vertical_deg = vertical_deg
        self._hole_axis = hole_axis

    def feed(self, sample: AccelSample, now: float | None = None) -> float | None:
        """
        Feed one sample; return the current angle, or None until trusted.

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

        # The elevation of the hole axis, folded to 0..90 (see _elevation_deg).
        target = _elevation_deg(unit, self._hole_axis)

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

        self.angle_deg = self._calibrate(self._elevation)
        return self.angle_deg

    def reset(self) -> None:
        """Forget everything (call when a stream session starts)."""
        self._elevation = None
        self._prev_time = None
        self.angle_deg = None

    # -- internals --------------------------------------------------------

    def _elapsed(self, now: float) -> float:
        """
        Seconds since the previous sample; 0 for the first, or after a gap.

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

    def _calibrate(self, raw_deg: float) -> float:
        """
        Map a raw elevation onto the calibrated 0..90 scale.

        A strictly monotonic affine map anchored at ``flat_deg`` -> 0 and
        ``vertical_deg`` -> 90, clamped at both ends. Monotonicity is what makes
        this honest rather than falsifying: distinct poses still read distinctly
        and no threshold information is lost, unlike a snap band that reports
        one value for a range of tilts. The smoothing above stays in raw units
        so its gap-based snap logic keeps its physical meaning, and at the
        default anchors this is the identity.
        """
        if raw_deg <= self._flat_deg:
            return 0.0
        if raw_deg >= self._vertical_deg:
            return 90.0
        span = self._vertical_deg - self._flat_deg
        return (raw_deg - self._flat_deg) * 90.0 / span
