"""Constants for colmi_ring."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "colmi_ring"

RING_ADDRESS_CONFIG_NAME = "ring_address"

CONF_HR_STALE_POLLS = "hr_stale_after_polls"
