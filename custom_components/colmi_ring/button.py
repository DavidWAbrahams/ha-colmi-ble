"""Button platform for colmi_ring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .api import ColmiRingApiClientError
from .const import CONF_DEVICE_FAMILY, FAMILY_COLMI, FAMILY_YCBT, LOGGER
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
    buttons: list[ColmiRingEntity] = []
    # Blink and Reboot are Colmi-only commands: the Yucheng client has no
    # equivalent, so those buttons would just fail when pressed.
    if entry.data.get(CONF_DEVICE_FAMILY, FAMILY_COLMI) != FAMILY_YCBT:
        buttons.extend(
            [
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
            ]
        )
    buttons.extend(
        [
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
    )
    # On-demand skin temperature exists only on the Yucheng protocol family;
    # the Colmi client has no temperature command path at all.
    if entry.data.get(CONF_DEVICE_FAMILY, FAMILY_COLMI) == FAMILY_YCBT:
        buttons.append(
            ColmiRingTemperatureButton(
                coordinator=coordinator,
                entity_description=ButtonEntityDescription(
                    key="colmi_ring_temperature_button",
                    name="Read Temperature",
                    icon="mdi:thermometer",
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


class ColmiRingTemperatureButton(ColmiRingEntity, ButtonEntity):
    """Trigger an on-demand skin-temperature measurement on the ring."""

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
        """Press the button (measurement takes up to a minute; background it)."""
        self._assert_ring_free()
        self.hass.async_create_task(self.async_press())

    async def async_press(self) -> None:
        """Take an on-demand skin-temperature reading and publish it."""
        client = self.coordinator.config_entry.runtime_data.client
        try:
            reading = await client.async_take_temperature()
        except ColmiRingApiClientError as err:
            LOGGER.warning("Colmi ring temperature measurement failed: %s", err)
            raise
        if reading.get("skin_temperature") is None:
            LOGGER.warning(
                "Colmi ring temperature measurement produced no valid reading "
                "(is the ring worn snugly on a finger?)"
            )
        self.coordinator.apply_skin_temperature(reading)
