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

# Import heart-rate samples recorded while out of BLE range into HA history
# (direct recorder states insert). Safe for the MEASUREMENT state class.
CONF_HR_BACKFILL = "hr_backfill"

# Experimental: same treatment for the steps/distance/calories sensors, whose
# TOTAL_INCREASING day totals are imported as cumulative bins. Raw history
# only (no long-term statistics for old ranges); see plans/hr-backfill-plan.md.
CONF_STEPS_BACKFILL = "steps_backfill"

# --- Motion / gesture detection -------------------------------------------
#
# Home Assistant event type fired by the gesture event entities and the
# custom event of the same name.
GESTURE_EVENT_TYPE = "colmi_ring_gesture"

# Everything that needs to open a connection of its own refuses with these while
# a session holds the ring's single GATT link (see MotionManager). One message
# per kind of session, so the refusal names the switch to turn off.
STREAM_BUSY_MESSAGE = "Unavailable while the motion stream is on"
WAVE_BUSY_MESSAGE = "Unavailable while wave detection is on"

# Opt-in: creates the motion switch and the gesture event entities. Off by
# default because streaming holds the single GATT link open (which pauses
# normal polling) and measurably costs battery.
CONF_MOTION_ENABLED = "motion_enabled"

# Multiplier on the detector thresholds: "low" sensitivity means a *bigger*
# movement is required. See GESTURE_SENSITIVITY_SCALE.
CONF_GESTURE_SENSITIVITY = "gesture_sensitivity"
DEFAULT_GESTURE_SENSITIVITY = "medium"

# Options value -> divisor applied to the base thresholds, so a larger number
# means a *smaller* threshold, i.e. more sensitive. "low" is therefore the
# fraction and "high" the multiple.
GESTURE_SENSITIVITY_SCALE: dict[str, float] = {
    "low": 0.6,
    "medium": 1.0,
    "high": 1.6,
}

# Minutes the RAW stream stays open before it is turned off automatically, so a
# forgotten switch cannot flatten the battery. Restarted on every gesture.
CONF_MOTION_STREAM_TIMEOUT = "motion_stream_timeout"
DEFAULT_MOTION_STREAM_TIMEOUT = 30

# Minutes the wave listener holds the ring's link before switching itself off.
# Its own option because the cost is different in kind: the wave listener does
# not stream, so it is meant to be left on, but while it is on the ring's single
# link is taken and heart rate, battery and steps stop updating. So it gets a
# longer default than the stream, and 0 means "leave it on until I say
# otherwise" (see CONF_MOTION_STREAM_TIMEOUT for the same convention).
CONF_WAVE_TIMEOUT = "wave_timeout"
DEFAULT_WAVE_TIMEOUT = 120

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

# Time constant of the leaky vertical-velocity integrator. The leak is what
# makes a slow, deliberate arm lift invisible while a snap raise is obvious.
MOTION_VELOCITY_LEAK_TAU_S = 0.45

# Peak |vertical velocity| (g*s, ~1 g*s = 9.8 m/s) that counts as a rapid
# vertical movement. 0.11 g*s is about 1.1 m/s.
MOTION_RAISE_VELOCITY_G_S = 0.11

# A *rapid* movement must also reach its peak velocity promptly. Without this,
# holding a steady push for two seconds eventually accumulates enough leaked
# velocity to count as a raise on a sensitive setting, even though the gesture
# is defined by its snap rather than its total impulse.
MOTION_RAISE_MAX_PEAK_S = 0.6

# The gravity direction must also have swung by this much between the start and
# the end of a vertical movement, so a straight up-and-down bounce (or a
# vibration) cannot pass for a raise. Deliberately well below the tens of
# degrees a wrist actually rotates: the ending heading is an average over a
# short window, which flattens the measured swing somewhat.
#
# Unlike the magnitude thresholds, this is NOT divided by the gesture
# sensitivity: a swing either happened or it did not, and relaxing it on a
# sensitive setting would remove the discrimination the setting depends on.
MOTION_RAISE_PITCH_DEG = 15.0
MOTION_PITCH_RECENT_WINDOW_S = 0.25

# Minimum interval between reports of the *same* gesture, and the shorter
# window in which one gesture suppresses the others (a hard raise ends with a
# lower, and either can bleed into the other's window).
MOTION_GESTURE_COOLDOWN_S = 1.2
MOTION_GESTURE_SUPPRESS_S = 0.5

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

# Report zero below this angle. A resting hand produces a little residual noise
# however well filtered, and the recorder does not need a row per wiggle; a ring
# resting horizontal reads exactly zero rather than a wander around it.
MOTION_VERTICAL_DEADBAND_DEG = 1.5

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
