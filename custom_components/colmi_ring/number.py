"""Number platform for colmi_ring: heart-rate sampling interval on the ring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTime

from .api import ColmiRingApiClientError
from .colmi_codec.hr_settings import HeartRateLogSettings
from .const import (
    CONF_DEVICE_FAMILY,
    CONF_YCBT_MONITOR_INTERVAL,
    DEFAULT_YCBT_MONITOR_INTERVAL,
    FAMILY_COLMI,
    FAMILY_YCBT,
    LOGGER,
    YCBT_MONITOR_INTERVAL_MAX,
    YCBT_MONITOR_INTERVAL_MIN,
    clamp_ycbt_monitor_interval,
)
from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry

ENTITY_DESCRIPTIONS = (
    NumberEntityDescription(
        key="colmi_ring_hr_log_interval",
        name="Heart Rate Sample Interval",
        icon="mdi:heart-pulse",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        # The ring's daily HR log is a 288-slot grid with one entry per
        # 5 minutes, so samples taken more often than that cannot all be
        # stored anyway; the firmware collapses them at write time. Values
        # below 5 minutes therefore only cost battery.
        native_min_value=5,
        native_max_value=255,
        native_step=1,
        # A 5..255 range is awkward as a slider; use a plain number box.
        mode=NumberMode.BOX,
    ),
)

# The Yucheng monitor payload is {enable, intervalMinutes}. The OEM SDK
# documents 1..240 minutes but its app clamps ring requests up to 30 - the
# firmware ignores anything below that - so the entity range matches what
# the hardware honours (see const.YCBT_MONITOR_INTERVAL_MIN).
YCBT_ENTITY_DESCRIPTION = NumberEntityDescription(
    key="colmi_ring_hr_log_interval",
    name="Heart Rate Sample Interval",
    icon="mdi:heart-pulse",
    native_unit_of_measurement=UnitOfTime.MINUTES,
    native_min_value=YCBT_MONITOR_INTERVAL_MIN,
    native_max_value=YCBT_MONITOR_INTERVAL_MAX,
    native_step=1,
    mode=NumberMode.BOX,
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    if entry.data.get(CONF_DEVICE_FAMILY, FAMILY_COLMI) == FAMILY_YCBT:
        # Same user-facing control, different backing: the family has no
        # interval *read* opcode, so the entity shows the value the
        # integration last had the ring apply (the entry options), not one
        # read back from the ring.
        async_add_entities(
            [
                YcbtRingMonitorIntervalNumber(
                    coordinator=entry.runtime_data.coordinator,
                    entity_description=YCBT_ENTITY_DESCRIPTION,
                )
            ]
        )
        return
    async_add_entities(
        ColmiRingHrIntervalNumber(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class YcbtRingMonitorIntervalNumber(ColmiRingEntity, NumberEntity):
    """
    Yucheng background-monitoring interval, backed by the entry options.

    The ring never reports its interval, so this entity is write-through:
    the value comes from the options the integration applies on every
    connect, and setting it writes the ring immediately *and* persists the
    new value (which reloads the entry, so future connects agree).
    """

    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: NumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    @property
    def native_value(self) -> float:
        """The interval the integration keeps the ring set to (minutes)."""
        return float(
            clamp_ycbt_monitor_interval(
                self.coordinator.config_entry.options.get(
                    CONF_YCBT_MONITOR_INTERVAL,
                    DEFAULT_YCBT_MONITOR_INTERVAL,
                )
            )
        )

    async def async_set_native_value(self, value: float) -> None:
        """Write the interval to the ring, then persist it in the options."""
        self._assert_ring_free()
        entry = self.coordinator.config_entry
        client = entry.runtime_data.client
        try:
            accepted = await client.async_set_monitor_interval(int(value))
        except ColmiRingApiClientError as err:
            LOGGER.warning(
                "Failed to set Yucheng ring monitor interval to %s: %s",
                value,
                err,
            )
            raise
        LOGGER.debug(
            "Yucheng ring monitor interval set to %s min; persisting option",
            accepted,
        )
        # Persisting the option fires our update listener, which reloads the
        # entry so the client factory and coordinator pick up the new value;
        # no explicit coordinator refresh is needed.
        self.hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_YCBT_MONITOR_INTERVAL: accepted},
        )


class ColmiRingHrIntervalNumber(ColmiRingEntity, NumberEntity):
    """Ring heart-rate logging interval, editable from the HA UI."""

    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: NumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    def _current_settings(self) -> HeartRateLogSettings | None:
        data = self.coordinator.data
        if not data:
            return None
        settings = data.get("hr_settings")
        return settings if isinstance(settings, HeartRateLogSettings) else None

    @property
    def native_value(self) -> float | None:
        """Current sampling interval reported by the ring (minutes)."""
        settings = self._current_settings()
        return float(settings.interval) if settings else None

    @property
    def available(self) -> bool:
        """Available when the ring has answered the settings query."""
        return super().available and self._current_settings() is not None

    async def async_set_native_value(self, value: float) -> None:
        """Write the interval to the ring and re-read it to verify."""
        self._assert_ring_free()
        settings = self._current_settings()
        # Preserve the ring's current enabled flag unless we know it.
        enabled = settings.enabled if settings else True
        client = self.coordinator.config_entry.runtime_data.client
        try:
            result = await client.async_set_hr_log_settings(
                HeartRateLogSettings(enabled=enabled, interval=int(value))
            )
        except ColmiRingApiClientError as err:
            LOGGER.warning("Failed to set Colmi ring HR interval to %s: %s", value, err)
            raise
        else:
            LOGGER.debug(
                "Colmi ring HR logging settings now: %s",
                result,
            )
            await self.coordinator.async_request_refresh()
