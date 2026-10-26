"""Sensor platform for colmi_ring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfLength
from homeassistant.core import callback

from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry

ENTITY_DESCRIPTIONS = (
    SensorEntityDescription(
        key="heart_rate",
        name="Heart Rate",
        icon="mdi:heart-pulse",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="battery_level",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="steps",
        name="Steps",
        icon="mdi:shoe-print",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="distance",
        name="Distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="calories",
        name="Calories",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    async_add_entities(
        ColmiRingSensor(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class ColmiRingSensor(ColmiRingEntity, SensorEntity):
    """colmi_ring Sensor class."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor class."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        if entity_description.key == "heart_rate":
            # Keep the unique id the HR sensor was originally registered with
            # (plain entry id from the base class) so its history/entity_id is
            # preserved when adding further sensors.
            self._attr_unique_id = coordinator.config_entry.entry_id
        else:
            self._attr_unique_id = (
                f"{coordinator.config_entry.entry_id}_{entity_description.key}"
            )

    @property
    def native_value(self) -> int | None:
        """Return the native value of the sensor."""
        data = self.coordinator.data
        if not data:
            return None
        return data.get(self.entity_description.key)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator (adds charging attribute)."""
        if (
            self.entity_description.key == "battery_level"
            and self.coordinator.last_update_success
            and self.coordinator.data
        ):
            charging = self.coordinator.data.get("battery_charging")
            if charging is not None:
                self._attr_extra_state_attributes = {"charging": bool(charging)}
        super()._handle_coordinator_update()
