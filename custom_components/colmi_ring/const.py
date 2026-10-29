"""Constants for colmi_ring."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "colmi_ring"

RING_ADDRESS_CONFIG_NAME = "ring_address"

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

# Everything that needs to open a connection of its own refuses with this while
# the motion stream holds the ring's single GATT link (see MotionManager).
STREAM_BUSY_MESSAGE = "Unavailable while the motion stream is on"

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

# Log every decoded accelerometer sample at DEBUG level (tuning aid).
CONF_MOTION_LOG_SAMPLES = "motion_log_samples"

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

# Stream-derived clap: a near-instantaneous spike of this much *linear*
# acceleration (gravity removed), rising within MOTION_CLAP_RISE_S and falling
# back within MOTION_CLAP_MAX_WIDTH_S. The width limit is what keeps a clench
# or a swung arm (hundreds of ms of sustained acceleration) from reading as a
# clap; a clap impulse is tens of milliseconds.
MOTION_CLAP_SPIKE_G = 1.1
MOTION_CLAP_RISE_S = 0.15
MOTION_CLAP_MAX_WIDTH_S = 0.25

# The ring must have been essentially still for this long before the spike.
# Without it a vibration (a shaking hand, a mower, a steering wheel) reads as a
# machine-gun of well-shaped impulses, because each half-cycle rises fast and
# falls fast. A clap is preceded by a still hand.
MOTION_CLAP_QUIET_S = 0.4

# The hand must also *ring down* after the spike and stay quiet for this long
# before the clap is reported. This is the test that separates a clap from the
# first half-cycle of a shake: with a still hand beforehand the two have
# near-identical shapes, but a clap is followed by a coasting hand (constant
# velocity is no acceleration) while a shake is followed by more shaking.
# Costs this much latency.
MOTION_CLAP_SETTLE_S = 0.2

# How far below its own peak the motion has to fall to count as ringing down, as
# well as how long it may take to get there. The counter-stroke of a real clap
# (the hand decelerating) is part of the clap, so an absolute "must be near 1 g"
# would reject genuine claps; relative to the impulse it still excludes a shake,
# whose next half-cycle returns at nearly full amplitude.
MOTION_CLAP_RING_DOWN_FRACTION = 0.25

# How long the impulse is allowed to take ringing down. A clap's own back stroke
# is over in tens of milliseconds and the hand coasts soon after, so this is
# generous at well under a second. It matters as much as the fraction: a shake
# keeps interrupting the ring-down, and without a deadline its very last
# half-cycle would eventually settle, be awarded the gesture, and be reported a
# beat after the shaking stopped.
MOTION_CLAP_RING_DOWN_S = 0.6

# How far the raw magnitude may stray from 1 g and still count as "still" for
# the window above. Measured on the magnitude rather than the gravity-projected
# acceleration on purpose: twice per cycle a vibration's linear acceleration
# dips back through zero, so only the magnitude says undeniably "this hand is
# moving". Deliberately generous, since a real wrist at rest is not perfectly
# quiet; tighten against captured data.
MOTION_CLAP_QUIET_BAND_G = 0.15

# A clap impulse lasts a few tens of ms and two claps in a row are a
# deliberate double-clap, not one gesture.
MOTION_CLAP_REFRACTORY_S = 0.6

# Minimum interval between reports of the *same* gesture, and the shorter
# window in which one gesture suppresses the others (a hard raise can also
# look like a clap).
MOTION_GESTURE_COOLDOWN_S = 1.2
MOTION_GESTURE_SUPPRESS_S = 0.5

# How long the RAW stream may stay silent before the session is considered
# dead and closed. Generous because the stock firmware is known to report
# slowly, and proxy latency adds to it.
MOTION_STREAM_SILENCE_TIMEOUT_S = 30.0

# Below this measured sample rate the stream cannot resolve a clap's ~50 ms
# impulse, so the stream-derived clap detector stays off and only raise/lower
# are reported. Best guess; the Phase 0 probe measures the real rate.
MOTION_MIN_HZ_FOR_STREAM_CLAP = 15.0

# Duration of the startup mute: the ring echoes our own gesture-enable packet
# back at us, which must not be read as a clap.
MOTION_STARTUP_MUTE_S = 2.0
