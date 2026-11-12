"""Constants for colmi_ring."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "colmi_ring"

RING_ADDRESS_CONFIG_NAME = "ring_address"

# --- Device family ----------------------------------------------------------
#
# Two ring protocol families share this integration. The family is decided
# once, at config time (the config flow probes the device), stored in the
# entry data, and read everywhere a platform or the client factory needs to
# know which protocol speaks to the ring. Old entries migrate to "colmi".
CONF_DEVICE_FAMILY = "device_family"
FAMILY_COLMI = "colmi"
FAMILY_YCBT = "ycbt"

CONF_HR_STALE_POLLS = "hr_stale_after_polls"

# Upper bound (minutes) for the adaptive BLE poll interval: the coordinator
# polls at the ring's HR sampling interval, but never slower than this
# option allows. See ColmiRingDataUpdateCoordinator._apply_poll_interval().
CONF_POLL_MAX_INTERVAL = "poll_max_interval"
DEFAULT_POLL_MAX_INTERVAL = 30

# Yucheng rings have no readable HR-sampling setting (no read opcode exists),
# so the integration's chosen background monitoring interval lives here in
# the entry options and is re-sent to the ring on every connect. The number
# entity writes it, the init burst applies it, and the coordinator matches
# its poll/staleness cadence to it.
CONF_YCBT_MONITOR_INTERVAL = "ycbt_monitor_interval"
DEFAULT_YCBT_MONITOR_INTERVAL = 30
# The monitor payload is {enable, intervalMinutes}; the OEM SDK documents
# 1..240 minutes but its app clamps ring requests up to 30 ("the ring's
# all-day sampler refuses intervals under 30 minutes"), with a 60-minute
# firmware default. Anything below 30 is silently ignored by the firmware,
# so the effective floor here matches what the hardware actually honours.
YCBT_MONITOR_INTERVAL_MIN = 30
YCBT_MONITOR_INTERVAL_MAX = 240


def clamp_ycbt_monitor_interval(value: object) -> int:
    """Coerce an option value to a valid Yucheng monitor interval (minutes).

    Missing/garbage option values fall back to the default so a hand-edited
    or half-migrated entry never yields an out-of-range wire value.
    """
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        minutes = DEFAULT_YCBT_MONITOR_INTERVAL
    return max(YCBT_MONITOR_INTERVAL_MIN, min(YCBT_MONITOR_INTERVAL_MAX, minutes))


# Import samples recorded on the ring while out of BLE range into HA history
# (direct recorder states insert). Heart rate is a plain MEASUREMENT and
# imports cleanly; the steps/distance/calories sensors are TOTAL_INCREASING
# day totals imported as cumulative bins (raw history only, no long-term
# statistics for old ranges); see plans/hr-backfill-plan.md.
CONF_BACKFILL_ON_RECONNECT = "backfill_on_reconnect"

# --- Motion / gesture detection -------------------------------------------
#
# Home Assistant event type fired by the gesture event entities and the
# custom event of the same name.
GESTURE_EVENT_TYPE = "colmi_ring_gesture"

# The two sessions refuse each other with these (see MotionManager): RAW mode
# takes the accelerometer for itself and silences the ring's camera-gesture
# reports, so the modes cannot run at once even though they now share one link.
# One message per kind of session, so the refusal names the switch to turn off.
STREAM_BUSY_MESSAGE = "Unavailable while the motion stream is on"
WAVE_BUSY_MESSAGE = "Unavailable while wave detection is on"

# The ring's optical sensor lights different LEDs per reading type, so the
# on-demand measurements (Read Heart Rate / Read SpO2 / Read Temperature /
# Read HRV) can only run one at a time. The coordinator's ``ring_busy`` flag
# carries the running measurement's name while the controls grey each other
# out.
MEASURE_HEART_RATE = "heart rate"
MEASURE_HRV = "heart rate variability"
MEASURE_SPO2 = "SpO2"
MEASURE_TEMPERATURE = "skin temperature"


def measurement_busy_message(running: str) -> str:
    """Why a fresh measurement cannot start while another holds the sensor."""
    return (
        "The ring can only take one measurement at a time:"
        f" {running} measurement in progress"
    )

# Seconds the hand must stay still for the stillness gesture. Longer than the
# flip hold on purpose: a resting hand should take its time announcing itself,
# so casual pauses do not fire the event.
CONF_GESTURE_HOLD_STILL_S = "gesture_hold_still_s"
DEFAULT_GESTURE_HOLD_STILL_S = 10.0

# Seconds each palm pose must be held for the flip gesture (palm up for X s,
# flipped, palm down for X s). Short because the flip is an intentional two-
# part gesture, not a resting state.
CONF_GESTURE_HOLD_FLIP_S = "gesture_hold_flip_s"
DEFAULT_GESTURE_HOLD_FLIP_S = 2.0

# Seconds the hand must stay still *and* raised for the raised-hand gesture.
# Shorter than the stillness hold: raising the hand is a deliberate act, so it
# announces itself faster than a resting hand is allowed to.
CONF_GESTURE_HOLD_RAISED_S = "gesture_hold_raised_s"
DEFAULT_GESTURE_HOLD_RAISED_S = 4.0

# Seconds each half of the hand chop must be held: vertical first, then
# horizontal. One option for both halves because the gesture is symmetric -
# it is the shift between the two poses that is the gesture.
CONF_GESTURE_HOLD_CHOP_S = "gesture_hold_chop_s"
DEFAULT_GESTURE_HOLD_CHOP_S = 2.0

# Minutes the RAW stream stays open before it is turned off automatically, so a
# forgotten switch cannot flatten the battery. Restarted on every gesture.
CONF_MOTION_STREAM_TIMEOUT = "motion_stream_timeout"
DEFAULT_MOTION_STREAM_TIMEOUT = 15

# Minutes the wave listener holds the ring's link before switching itself off.
# Its own option because the cost is different in kind: the wave listener does
# not stream, so it is meant to be left on, but while it is on the ring's single
# link is taken and heart rate, battery and steps stop updating. So it gets a
# longer default than the stream, and 0 means "leave it on until I say
# otherwise" (see CONF_MOTION_STREAM_TIMEOUT for the same convention).
CONF_WAVE_TIMEOUT = "wave_timeout"
DEFAULT_WAVE_TIMEOUT = 0

# Log every decoded accelerometer sample at DEBUG level (tuning aid).
CONF_MOTION_LOG_SAMPLES = "motion_log_samples"

# Minimum seconds between updates of the accelerometer sensors; 0 publishes every
# sample. Configurable because the stream rate is a firmware property rather than
# a fixed one: stock R02 firmware reports at 1 Hz, where any throttle is
# invisible, while a firmware that reports several samples per second is throttled
# by this. Every sensor update is a recorder row, so four axes at 25 Hz would
# write a hundred rows per second; the one-second default keeps the database
# usable, and CONF_MOTION_LOG_SAMPLES remains the way to see the full-rate signal.
CONF_MOTION_SENSOR_UPDATE_S = "motion_sensor_update_s"
DEFAULT_MOTION_SENSOR_UPDATE_S = 1.0

# --- Detector tuning ------------------------------------------------------
#
# All of these are best guesses pending the Phase 0 capture; see
# plans/motion-gestures-plan.md.

# Time constant of the gravity estimate (slow exponential average of the raw
# vector). Long enough that a gesture does not tilt the reference frame, short
# enough to follow the wrist as it settles.
MOTION_GRAVITY_TAU_S = 3.0

# When the raw magnitude sits within this much of 1 g, the ring is not
# accelerating, so the raw vector is itself a good gravity estimate and the
# filter is allowed to converge fast (see MOTION_GRAVITY_FAST_TAU_S). Without
# this, a wrist that changes pose while the slow estimate is still pointing the
# old way looks like a long, slow acceleration for as much as a second: exactly
# what happens when a stream resumes after a gap, or after samples are dropped.
MOTION_GRAVITY_TRUST_G = 0.2

# Time constant used while the magnitude says "not accelerating". Short enough
# to absorb a pose change, long enough that the brief moment a real movement
# spends near 1 g (its very first samples) is not eaten.
MOTION_GRAVITY_FAST_TAU_S = 0.08

# A sample counts as *still* when the gravity-removed acceleration stays under
# this much. A walking or gesturing hand shakes by far more than this; a held
# pose, even a slightly unsteady one, sits well inside it.
MOTION_STILL_ACCEL_G = 0.25

# A held pose tolerates this much wander from the direction it was anchored to
# before the hold is considered broken. Comfortably above sensor and hand
# jitter, comfortably below the tens of degrees of a deliberate pose change.
MOTION_HOLD_TOLERANCE_DEG = 20.0

# The two poses of a flip must be at least this far apart. A palm-up to
# palm-down rotation is 180 degrees; the margin below it absorbs a flip that is
# not quite complete and the heading error of a single unsmoothed sample.
# Deliberately far from the hold tolerance: an angle between the two belongs to
# neither, and ends the old hold without ever completing a new one.
MOTION_FLIP_MIN_ANGLE_DEG = 120.0

# The turn between the two holds of a flip must not take longer than this.
# It is a floor: the detector uses max(this, MOTION_FLIP_TRANSITION_DT_FACTOR
# times the measured sample interval), because a stream that reports once per
# second cannot observe a transition of under a second at all - one second is
# the shortest measurable transition, not a license to accept slow ones.
MOTION_FLIP_TRANSITION_BASE_S = 1.0
MOTION_FLIP_TRANSITION_DT_FACTOR = 2.0

# A longer silence than this between samples is a gap (reconnect, dropped
# burst), not a measurement: it ends the current hold, re-arms the stillness
# gesture, and must not move the gravity estimate or the rate estimate.
MOTION_SAMPLE_GAP_MAX_S = 5.0

# --- Vertical angle tuning --------------------------------------------------
#
# See :class:`VerticalAngleTracker`. The RAW stream carries no gyroscope, so the
# only pose information in it is the direction of gravity in the sensor's own
# frame; the reported angle is how far the ring's hole axis is deflected from
# the horizontal plane, measured absolutely against gravity.

# Which sensor axis runs through the ring hole. The reported angle is the
# elevation of this axis above the horizontal plane, so the axis choice is the
# whole calibration: if the sensor reads 90 degrees with the hole axis flat on
# a table (and 0 held upright), the firmware maps the axes differently and this
# one constant is the only thing to change.
MOTION_VERTICAL_AXIS = "z"

# Time constant of the low-pass applied to the elevation angle. The angle is
# smoothed rather than read from each raw sample: the accelerometer's least
# significant bit is 4 g / 2048, which at 1 g is about 0.11 degrees, and asin
# has an infinite derivative at +/-1 - so raw samples would jitter by several
# degrees around a hand that is not moving at all.
MOTION_VERTICAL_TAU_S = 0.5

# Once the raw target is this far from the current estimate, follow it with the
# short time constant below. Without this a large, deliberate change of pose -
# laying a vertical ring flat, say - looks like the reading is stuck, because
# an exponential chases the last part of a step slowly, and the flip itself is
# excluded by the trust gate, so the chase only starts once the hand settles.
# Above this divergence the movement is unambiguous, not noise, so following it
# quickly costs nothing in stability.
MOTION_VERTICAL_SNAP_DEG = 15.0

# Time constant used while the estimate and the raw target disagree by more
# than MOTION_VERTICAL_SNAP_DEG. Short enough that a snap of the wrist reads as
# one move rather than a slow slide.
MOTION_VERTICAL_SNAP_TAU_S = 0.1

# Discard samples whose magnitude strays this far from 1 g. Their direction is
# then mostly the movement rather than gravity, so letting them in would drag the
# pose estimate toward wherever the hand happens to be accelerating; the estimate
# simply holds its value while they last.
MOTION_VERTICAL_TRUST_G = 0.3

# How high (degrees, on the Vertical Angle sensor's calibrated scale) the hole
# axis must point for the hand to count as raised in the raised-hand gesture.
# Comfortably below the 90 a fully vertical finger reads, so a natural raised
# hand with a slightly bent finger still qualifies, and comfortably above the
# ~45 of a hand held out in front.
MOTION_RAISED_HAND_MIN_DEG = 70.0

# --- Hand chop tuning -------------------------------------------------------
#
# The chop is a move between two elevation bands: it starts above the raised
# band, drops through the gap between the bands (which is not part of either
# hold), and ends below the flat band. The bands are deliberately separated:
# a hand resting somewhere between them belongs to neither half of the
# gesture, and the transition window expires the armed state on its own.

# How high the hole axis must be held (degrees, calibrated scale) to arm the
# chop. Same bar as the raised-hand gesture: the user raises the hand first.
MOTION_CHOP_RAISED_MIN_DEG = 70.0

# How flat (degrees, calibrated scale) the hole axis must land for the chop to
# complete. Comfortably below the raised bar and below the gap: a hand laid
# flat on a table reads well inside it, while one merely lowered to chest
# height does not.
MOTION_CHOP_FLAT_MAX_DEG = 20.0

# --- Vertical angle calibration --------------------------------------------
#
# The reported elevation is a straight rescale of the filtered one, anchored at
# both ends: the raw elevation that means "flat" maps to 0, and the raw elevation
# that means "vertical" maps to 90, linearly in between. A monotonic map loses
# nothing - distinct poses still read distinctly, and nothing saturates except
# the two endpoints - while a snap band would have to assert a precision the
# hardware does not have.
#
# The high end matters because ``asin`` amplifies horizontal error without limit:
# at 1 g, 0.05 g of horizontal reading - sensor bias, cross-axis error, or a hand
# that is not perfectly still - pins the raw elevation near 87 degrees no matter
# how well the signal is filtered, because the offset is a nonzero mean rather
# than noise. Averaging cannot remove it; only an anchor can.
#
# Both are options rather than constants because the offset is a property of the
# unit and the wear, not of the code. Both default to the uncorrected map: the
# raw elevation already reads 0 flat and 90 upright.

# Raw elevation (degrees) that reads as flat, i.e. is reported as 0.
CONF_VERTICAL_FLAT_DEG = "vertical_flat_deg"
DEFAULT_VERTICAL_FLAT_DEG = 0.0

# Raw elevation (degrees) that reads as vertical, i.e. is reported as 90.
CONF_VERTICAL_UPRIGHT_DEG = "vertical_upright_deg"
DEFAULT_VERTICAL_UPRIGHT_DEG = 90.0

# How far either anchor may move from its uncorrected value. Beyond these the
# map would gain so much that ordinary readings stop being meaningful: the flat
# end may be pushed up to 20, while the upright end may be pulled down to 70 so
# a hand that never reads fully vertical still reaches 90.
VERTICAL_FLAT_ANCHOR_MAX_DEG = 20.0
VERTICAL_UPRIGHT_ANCHOR_MIN_DEG = 70.0


def resolve_vertical_anchors(flat: object, upright: object) -> tuple[float, float]:
    """Coerce the two anchor options into a valid ``(flat, vertical)`` pair.

    Garbage, or an upright anchor outside the usable 70..90 range, falls back to
    the uncorrected default rather than to a plausible-looking map, because a
    silently wrong calibration is worse than an obviously uncorrected one. That
    also covers entries saved before the upright anchor had a real default. A
    pair that cannot define an increasing map - the flat anchor at or above the
    vertical one - is rejected outright.
    """
    try:
        flat_deg = float(flat)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        flat_deg = DEFAULT_VERTICAL_FLAT_DEG
    try:
        upright_deg = float(upright)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        upright_deg = DEFAULT_VERTICAL_UPRIGHT_DEG

    flat_deg = min(max(flat_deg, 0.0), VERTICAL_FLAT_ANCHOR_MAX_DEG)
    if not VERTICAL_UPRIGHT_ANCHOR_MIN_DEG <= upright_deg <= 90.0:
        upright_deg = DEFAULT_VERTICAL_UPRIGHT_DEG

    if upright_deg <= flat_deg:
        return 0.0, 90.0
    return flat_deg, upright_deg

# How long the RAW stream may stay silent before the session is considered
# dead and closed. Generous because the stock firmware is known to report
# slowly, and proxy latency adds to it.
#
# Applies to the RAW stream only. The wave listener must never use it: silence is
# that session's normal state, often for hours at a time.
MOTION_STREAM_SILENCE_TIMEOUT_S = 30.0

# Minimum seconds between published waves from the ring's own detector.
#
# The detector is in the firmware, so a burst of report packets for one wave is
# something we can only smooth here: ``MotionManager._on_wave_packet`` is a
# report translation, not a detector, so without this a ring that repeats itself
# fires one HA event per packet. Set below the gap a deliberate double-wave has
# in practice, so two real waves stay two events.
WAVE_REPORT_COOLDOWN_S = 0.8
