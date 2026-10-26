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
# (recorder.import_entity_history). Safe for the MEASUREMENT state class.
CONF_HR_BACKFILL = "hr_backfill"

# Experimental: same treatment for the steps/distance/calories sensors, whose
# TOTAL_INCREASING day totals are imported as cumulative bins. Raw history
# only (no long-term statistics for old ranges); see plans/hr-backfill-plan.md.
CONF_STEPS_BACKFILL = "steps_backfill"
