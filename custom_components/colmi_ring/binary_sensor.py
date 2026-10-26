"""Binary sensor platform for colmi_ring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry

ENTITY_DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="on_finger",
        name="On Finger",
        device_class=BinarySensorDeviceClass.PRESENCE,
        icon="mdi:ring",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    async_add_entities(
        ColmiRingOnFingerBinarySensor(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class ColmiRingOnFingerBinarySensor(ColmiRingEntity, BinarySensorEntity):
    """Derived wear-state sensor.

    Fusion rule:
    - Off while the ring is charging (it is on the charger, not a finger).
    - Else On when the most recent poll produced a heart-rate sample > 0.
    - Else Off.

    Because heart rate is already cleared to None once the newest sample goes
    stale (see the coordinator staleness check), this sensor turns Off about
    one logging interval after the ring leaves the finger without any extra
    BLE traffic.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor class."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the ring is being worn."""
        data = self.coordinator.data
        if not data:
            return None
        # Charging wins: a ring on the charger is not on a finger, even if
        # the last (pre-removal) heart-rate sample is still fresh.
        if data.get("battery_charging"):
            return False
        heart_rate = data.get("heart_rate")
        return heart_rate is not None and heart_rate > 0
