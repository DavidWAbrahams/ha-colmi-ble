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
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import callback

from .const import (
    CONF_DEVICE_FAMILY,
    FAMILY_YCBT,
    LOGGER,
)
from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

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
    # HRV in milliseconds; both families fill it, by different means: the
    # Yucheng all-metrics history stream, or a Colmi on-demand real-time
    # reading (the Read HRV button). No device class fits HRV, so it rides
    # on a plain measurement sensor.
    SensorEntityDescription(
        key="hrv",
        name="Heart Rate Variability",
        icon="mdi:heart-multiple",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
)

# Skin temperature comes from the Yucheng all-metrics history stream (and
# the on-demand temperature session), neither of which the Colmi protocol
# client implements, so the sensor is created for YCBT entries only.
YCBT_ENTITY_DESCRIPTIONS = (
    SensorEntityDescription(
        key="skin_temperature",
        name="Skin Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
)

# Stress comes from the Colmi daily stress ("pressure") history log, which the
# Yucheng protocol client does not implement, so this sensor is created for
# Colmi entries only. The ring fills one 0-100 score per 30-minute slot.
COLMI_ENTITY_DESCRIPTIONS = (
    SensorEntityDescription(
        key="stress",
        name="Stress",
        icon="mdi:brain",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
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

# How far the ring's hole axis is deflected from the horizontal plane, measured
# absolutely against gravity. This is the one derived value among the
# accelerometer entities, and it is deliberately its own sensor rather than
# another face of the raw sample above: the axes stay exactly as the ring sent
# them, while this number is filtered and orientation-independent.
#
# No device class and no unit enum: the angle is a plain degree count against
# gravity, not an orientation the ACCELEROMETER or ROTATION classes mean, and
# the plain "°" unit keeps the import safe on older cores.
VERTICAL_ANGLE_SENSOR = SensorEntityDescription(
    key="vertical_angle",
    name="Vertical Angle",
    icon="mdi:angle-acute",
    native_unit_of_measurement="°",
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: ColmiRingDataUpdateCoordinator = entry.runtime_data.coordinator
    descriptions = list(ENTITY_DESCRIPTIONS)
    if entry.data.get(CONF_DEVICE_FAMILY) == FAMILY_YCBT:
        descriptions.extend(YCBT_ENTITY_DESCRIPTIONS)
    else:
        descriptions.extend(COLMI_ENTITY_DESCRIPTIONS)
    async_add_entities(
        ColmiRingSensor(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in descriptions
    )
    async_add_entities([ColmiRingLastSeenSensor(coordinator=coordinator)])
    # The accelerometer sensors only have data while the RAW stream is on, which
    # is up to the user to switch on. Yucheng rings have no motion manager at
    # all, so they are gated out.
    if entry.runtime_data.motion is None:
        return
    async_add_entities(
        ColmiRingAccelerometerSensor(coordinator=coordinator, sensor=sensor)
        for sensor in ACCELEROMETER_SENSORS
    )
    async_add_entities(
        [
            ColmiRingVerticalAngleSensor(
                coordinator=coordinator, description=VERTICAL_ANGLE_SENSOR
            )
        ]
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
    def native_value(self) -> int | float | None:
        """Return the native value of the sensor."""
        data = self.coordinator.data
        if not data:
            return None
        return data.get(self.entity_description.key)

    @property
    def available(self) -> bool:
        """HRV is unavailable on Colmi firmware that reports no HRV support.

        Only a definite ``supports_hrv`` of ``False`` hides the sensor: Yucheng
        rings source HRV from their history stream and never report the
        capability, and a Colmi ring that has not answered the capability
        query yet leaves the key ``None``.
        """
        if (
            self.entity_description.key == "hrv"
            and (self.coordinator.data or {}).get("supports_hrv") is False
        ):
            return False
        return super().available

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
            elif self.entity_description.key == "skin_temperature":
                timestamp = self.coordinator.data.get(
                    "skin_temperature_timestamp"
                )
                self._attr_extra_state_attributes = {"timestamp": timestamp}
            elif self.entity_description.key == "hrv":
                timestamp = self.coordinator.data.get("hrv_timestamp")
                self._attr_extra_state_attributes = {"timestamp": timestamp}
            elif self.entity_description.key == "stress":
                timestamp = self.coordinator.data.get("stress_timestamp")
                self._attr_extra_state_attributes = {"timestamp": timestamp}
        super()._handle_coordinator_update()


class ColmiRingLastSeenSensor(ColmiRingEntity, SensorEntity):
    """When this ring was last heard from, to the second.

    Pushed rather than polled: the coordinator notes every advertisement HA's
    scanner sees and every GATT notification the clients receive, throttled to
    one update per second (the timestamp is floored to whole seconds, so
    repeats within a second are no-ops). Deliberately not tied to the
    coordinator's data refresh: the ring is visible long between polls, and a
    poll failure must not make the ring look unseen.

    Disabled by default: even throttled, this entity can write a recorder row
    every second whenever the ring is in range, which is far more history than
    most users want to pay for. Enable it per-install if second-granular
    presence history is wanted; its availability and recent value are useful
    even while disabled, via the entity's diagnostics.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Name and unique-id the sensor (the seen signal comes in added)."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_last_seen"
        self._attr_name = "Last Seen"

    @callback
    def _async_on_seen(self, _seen: datetime) -> None:
        """The ring was just heard from; repaint now, not at the next poll."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to the (throttled) seen signal once fully added."""
        await super().async_added_to_hass()
        # Subscribed here rather than in __init__: a seen event can fire from
        # the bluetooth scanner at any moment, and writing state before the
        # entity is registered would raise.
        self.async_on_remove(
            self.coordinator.add_seen_listener(self._async_on_seen)
        )

    @property
    def native_value(self) -> datetime | None:
        """The last (second-floored) time any traffic from the ring arrived."""
        return self.coordinator.last_seen


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


class ColmiRingVerticalAngleSensor(ColmiRingEntity, SensorEntity):
    """How far the ring's hole axis is tilted from horizontal.

    Push-driven like the axis sensors, but reading the vertical angle tracker's
    answer rather than a raw sample. The value is the elevation, in degrees, of
    the axis through the ring hole above the horizontal plane, always between
    ``0`` and ``90``: ``0`` when the hole axis is horizontal (the ring is being
    held flat, with little or no gravity along that axis), ``90`` when it points
    at the sky or the floor (about 1 g along it, either sign).

    Three properties are worth knowing before building an automation on it:

    * it is absolute. Nothing is zeroed against a starting pose, so the reading
      cannot drift and needs no calibration;
    * it is insensitive to rotation in the horizontal plane: spinning the ring
      like a doorknob (or turning the hand) leaves the elevation of the axis
      alone, which is a property of gravity, not of this code;
    * it folds the two vertical ends together: the ring tip pointing up and
      pointing down both read 90.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the vertical angle sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{description.key}"
        )

    @property
    def native_value(self) -> float | None:
        """Return the current angle, or None until the stream has a sample."""
        motion = self._motion_manager()
        return motion.last_angle_deg if motion is not None else None

    async def async_added_to_hass(self) -> None:
        """Start following the stream once the entity is registered."""
        await super().async_added_to_hass()
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - setup order guard
            LOGGER.debug(
                "Colmi vertical angle sensor %s has no motion manager",
                self.entity_description.key,
            )
            return
        self.async_on_remove(motion.add_sample_listener(self._on_sample))

    @callback
    def _on_sample(self) -> None:
        """The manager published a sample; republish our angle from it."""
        self.async_write_ha_state()
