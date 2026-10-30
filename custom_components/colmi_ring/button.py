"""Button platform for colmi_ring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.exceptions import HomeAssistantError

from .api import ColmiRingApiClientError
from .const import CONF_MOTION_ENABLED, LOGGER
from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinator: ColmiRingDataUpdateCoordinator = entry.runtime_data.coordinator
    buttons: list[ColmiRingEntity] = [
        ColmiRingBlinkButton(
            coordinator=coordinator,
            entity_description=ButtonEntityDescription(
                key="colmi_ring_blink_button",
                name="Blink LED",
                icon="mdi:eye-outline",
            ),
        ),
        ColmiRingRebootButton(
            coordinator=coordinator,
            entity_description=ButtonEntityDescription(
                key="colmi_ring_reboot_button",
                name="Reboot",
                icon="mdi:restart",
            ),
        ),
        ColmiRingSpo2Button(
            coordinator=coordinator,
            entity_description=ButtonEntityDescription(
                key="colmi_ring_spo2_button",
                name="Read SpO2",
                icon="mdi:lungs",
            ),
        ),
        ColmiRingHeartRateButton(
            coordinator=coordinator,
            entity_description=ButtonEntityDescription(
                key="colmi_ring_heart_rate_button",
                name="Read Heart Rate",
                icon="mdi:heart-pulse",
            ),
        ),
    ]
    # Re-zeroing the ring angle only makes sense alongside the sensor it
    # calibrates, and that sensor is gated on the motion option.
    if entry.options.get(CONF_MOTION_ENABLED, False):
        buttons.append(
            ColmiRingCalibrateAngleButton(
                coordinator=coordinator,
                entity_description=ButtonEntityDescription(
                    key="calibrate_ring_angle",
                    name="Calibrate Ring Angle",
                    icon="mdi:angle-acute",
                ),
            )
        )
    async_add_entities(buttons)


class ColmiRingBlinkButton(ColmiRingEntity, ButtonEntity):
    """colmi_ring button class."""

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button class."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    def press(self) -> None:
        """Press the button."""
        # Checked here, not in async_press: these buttons fire their work in the
        # background, so raising from the task would only ever reach the log
        # instead of the UI.
        self._assert_ring_free()
        self.hass.async_create_task(self.async_press())

    async def async_press(self) -> None:
        """Press the button."""
        await self.coordinator.config_entry.runtime_data.client.async_blink_twice()


class ColmiRingRebootButton(ColmiRingEntity, ButtonEntity):
    """colmi_ring button class."""

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button class."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    def press(self) -> None:
        """Press the button."""
        self._assert_ring_free()
        self.hass.async_create_task(self.async_press())

    async def async_press(self) -> None:
        """Press the button."""
        await self.coordinator.config_entry.runtime_data.client.async_reboot()


class ColmiRingSpo2Button(ColmiRingEntity, ButtonEntity):
    """Trigger an on-demand SpO2 measurement on the ring."""

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button class."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    def press(self) -> None:
        """Press the button (measurement takes ~30s; run it in the background)."""
        self._assert_ring_free()
        self.hass.async_create_task(self.async_press())

    async def async_press(self) -> None:
        """Take an on-demand SpO2 reading and publish it to the sensor."""
        client = self.coordinator.config_entry.runtime_data.client
        try:
            reading = await client.async_take_spo2()
        except ColmiRingApiClientError as err:
            LOGGER.warning("Colmi ring SpO2 measurement failed: %s", err)
            raise
        if reading.get("spo2") is None:
            LOGGER.warning(
                "Colmi ring SpO2 measurement produced no valid reading "
                "(is the ring worn snugly on a finger?)"
            )
        self.coordinator.apply_spo2(reading)


class ColmiRingHeartRateButton(ColmiRingEntity, ButtonEntity):
    """Trigger an on-demand real-time heart-rate measurement on the ring."""

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button class."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    def press(self) -> None:
        """Press the button (measurement takes ~30s; run it in the background)."""
        self._assert_ring_free()
        self.hass.async_create_task(self.async_press())

    async def async_press(self) -> None:
        """Take an on-demand live HR reading and publish it to the sensor."""
        client = self.coordinator.config_entry.runtime_data.client
        try:
            reading = await client.async_take_heart_rate()
        except ColmiRingApiClientError as err:
            LOGGER.warning("Colmi ring heart-rate measurement failed: %s", err)
            raise
        if reading.get("heart_rate") is None:
            LOGGER.warning(
                "Colmi ring heart-rate measurement produced no valid reading "
                "(is the ring worn snugly on a finger?)"
            )
        self.coordinator.apply_heart_rate(reading)


class ColmiRingCalibrateAngleButton(ColmiRingEntity, ButtonEntity):
    """Re-zero the Ring Angle sensor on the pose the hand is in right now.

    The one button that deliberately does *not* call :meth:`_assert_ring_free`:
    re-zeroing mid-stream is the whole point. It writes nothing to the ring and
    opens no connection, so the stream keeps running and the sensor simply
    restarts counting from wherever the hand happens to be.

    It overrides :meth:`async_press` rather than ``press`` because re-zeroing
    republishes the angle sensor through the motion manager's callback
    listeners, which has to happen on the event loop; unlike the other buttons
    there is nothing slow to hand to a background task.
    """

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button class."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        )

    async def async_press(self) -> None:
        """Zero the angle on the pose the hand is in right now."""
        motion = self._motion_manager()
        if motion is None or not motion.async_calibrate_angle():
            # Only reachable before the stream has produced a usable sample,
            # i.e. in the first second of a session, or with the switch off.
            raise HomeAssistantError(
                "Colmi ring angle cannot be calibrated yet: the motion stream"
                " has not reported an accelerometer sample"
            )
        LOGGER.debug("Colmi ring angle zeroed on the current pose")
