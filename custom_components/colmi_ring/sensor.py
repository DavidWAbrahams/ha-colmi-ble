"""Sensor platform for colmi_ring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfLength
from homeassistant.core import callback

from .const import CONF_MOTION_ENABLED, LOGGER
from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .colmi_codec.motion import AccelSample
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
    SensorEntityDescription(
        key="spo2",
        name="Blood Oxygen",
        icon="mdi:lungs",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


@dataclass(frozen=True, slots=True)
class _AccelSensor:
    """One face of the raw sample: how to describe it, and which part to read."""

    description: SensorEntityDescription
    field: str


# The raw accelerometer stream, kept as raw as the ring sends it: one sensor per
# axis, in the ring's own frame, in units of g (gravity included). No filtering,
# no tilt solving, no gesture logic -- that is what the gesture events are for.
#
# Deliberately no device class or unit enum here: the codec's unit is simply "g",
# and the ACCELERATION device class is newer than some cores this integration
# runs on, so depending on it would risk failing the whole sensor platform at
# import for a cosmetic gain. The axis icons do carry the meaning.
ACCELEROMETER_SENSORS: tuple[_AccelSensor, ...] = (
    _AccelSensor(
        SensorEntityDescription(
            key="accel_x",
            name="Acceleration X",
            icon="mdi:axis-x-arrow",
            native_unit_of_measurement="g",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
        ),
        field="x",
    ),
    _AccelSensor(
        SensorEntityDescription(
            key="accel_y",
            name="Acceleration Y",
            icon="mdi:axis-y-arrow",
            native_unit_of_measurement="g",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
        ),
        field="y",
    ),
    _AccelSensor(
        SensorEntityDescription(
            key="accel_z",
            name="Acceleration Z",
            icon="mdi:axis-z-arrow",
            native_unit_of_measurement="g",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
        ),
        field="z",
    ),
    _AccelSensor(
        SensorEntityDescription(
            key="accel_magnitude",
            name="Acceleration",
            icon="mdi:speedometer",
            native_unit_of_measurement="g",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=3,
        ),
        field="magnitude_g",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: ColmiRingDataUpdateCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        ColmiRingSensor(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )
    # The accelerometer sensors only have data while the RAW stream is on, which
    # only happens with gestures enabled, so that option gates them too.
    if not entry.options.get(CONF_MOTION_ENABLED, False):
        return
    async_add_entities(
        ColmiRingAccelerometerSensor(coordinator=coordinator, sensor=sensor)
        for sensor in ACCELEROMETER_SENSORS
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
        """Handle updated data (adds battery charging / spo2 timestamp attrs)."""
        if self.coordinator.last_update_success and self.coordinator.data:
            if self.entity_description.key == "battery_level":
                charging = self.coordinator.data.get("battery_charging")
                if charging is not None:
                    self._attr_extra_state_attributes = {
                        "charging": bool(charging)
                    }
            elif self.entity_description.key == "spo2":
                timestamp = self.coordinator.data.get("spo2_timestamp")
                self._attr_extra_state_attributes = {"timestamp": timestamp}
        super()._handle_coordinator_update()


class ColmiRingAccelerometerSensor(ColmiRingEntity, SensorEntity):
    """One axis (or the magnitude) of the ring's live RAW accelerometer stream.

    Push-driven instead of polled: these values only exist while the connection
    is held open with the stream on, so the motion manager says "there is a new
    sample" and we re-read :attr:`MotionManager.last_sample`. Consequently the
    sensors are ``unknown`` whenever the Motion Stream switch is off, which is
    the honest answer rather than a stale last reading.

    The number is whatever the ring sent, divided only by the full-scale
    conversion: no low-pass filter, no gravity removal, no orientation solving.
    Expect roughly ``1.0 g`` on whichever axis points up when the hand is still.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        sensor: _AccelSensor,
    ) -> None:
        """Initialize one accelerometer sensor."""
        super().__init__(coordinator)
        self.entity_description = sensor.description
        self._field = sensor.field
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{sensor.description.key}"
        )
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def native_value(self) -> float | None:
        """Return the current value of this axis, or None between sessions."""
        motion = self._motion_manager()
        sample: AccelSample | None = motion.last_sample if motion else None
        if sample is None:
            return None
        return getattr(sample, self._field)

    async def async_added_to_hass(self) -> None:
        """Start following the stream once the entity is registered."""
        await super().async_added_to_hass()
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - setup order guard
            LOGGER.debug(
                "Colmi ring accelerometer sensor %s has no motion manager",
                self.entity_description.key,
            )
            return
        self._unsubscribe = motion.add_sample_listener(self._on_sample)
        self.async_on_remove(self._unsubscribe)

    @callback
    def _on_sample(self) -> None:
        """The manager published a sample; republish our part of it."""
        self.async_write_ha_state()
