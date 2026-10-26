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

ENTITY_DESCRIPTIONS = {
    description.key: description
    for description in (
        BinarySensorEntityDescription(
            key="on_finger",
            name="On Finger",
            device_class=BinarySensorDeviceClass.PRESENCE,
            icon="mdi:ring",
        ),
        # HA's "Battery is (not) charging" condition only lists binary
        # sensors with device class ``battery_charging`` (on = charging).
        # The charging flag previously lived only as an attribute on the
        # battery sensor, which those consumers cannot see. (Note the
        # binary-sensor ``battery`` class means *low battery*, not charging.)
        BinarySensorEntityDescription(
            key="battery_charging",
            name="Charging",
            device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        ),
    )
}


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            ColmiRingOnFingerBinarySensor(
                coordinator=coordinator,
                entity_description=ENTITY_DESCRIPTIONS["on_finger"],
            ),
            ColmiRingBatteryChargingBinarySensor(
                coordinator=coordinator,
                entity_description=ENTITY_DESCRIPTIONS["battery_charging"],
            ),
        ]
    )


class ColmiRingOnFingerBinarySensor(ColmiRingEntity, BinarySensorEntity):
    """Derived wear-state sensor.

    Fusion rule:
    - Off while the ring is charging (it is on the charger, not a finger).
    - Else Off when the ring's most recent HR log slot read 0 bpm. This is
      direct off-finger evidence and bypasses the heart-rate staleness grace.
    - Else On when the most recent poll produced a fresh heart-rate sample > 0.
    - Else Off.
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
        # A 0-bpm reading in the ring's newest log slot means the optical
        # sensor saw no pulse at its most recent sample. This is stronger
        # than the staleness grace: even while the last non-zero sample is
        # still "fresh", the ring is demonstrably off the finger now.
        if data.get("hr_zero_reading"):
            return False
        heart_rate = data.get("heart_rate")
        return heart_rate is not None and heart_rate > 0


class ColmiRingBatteryChargingBinarySensor(ColmiRingEntity, BinarySensorEntity):
    """Charging-state sensor.

    Exposes the ring's raw charging flag as a ``battery_charging``
    device-class binary sensor (on = charging) so it is visible to HA's
    battery-charging conditions.
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
        """Return True if the ring is charging."""
        data = self.coordinator.data
        if not data:
            return None
        charging = data.get("battery_charging")
        return None if charging is None else bool(charging)
