"""Button platform for colmi_ring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .api import ColmiRingApiClientError
from .const import LOGGER
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
    async_add_entities(
        [
            ColmiRingBlinkButton(
                coordinator=entry.runtime_data.coordinator,
                entity_description=ButtonEntityDescription(
                    key="colmi_ring_blink_button",
                    name="Blink LED",
                    icon="mdi:eye-outline",
                ),
            ),
            ColmiRingRebootButton(
                coordinator=entry.runtime_data.coordinator,
                entity_description=ButtonEntityDescription(
                    key="colmi_ring_reboot_button",
                    name="Reboot",
                    icon="mdi:restart",
                ),
            ),
            ColmiRingSpo2Button(
                coordinator=entry.runtime_data.coordinator,
                entity_description=ButtonEntityDescription(
                    key="colmi_ring_spo2_button",
                    name="Read SpO2",
                    icon="mdi:drop",
                ),
            ),
            ColmiRingHeartRateButton(
                coordinator=entry.runtime_data.coordinator,
                entity_description=ButtonEntityDescription(
                    key="colmi_ring_heart_rate_button",
                    name="Read Heart Rate",
                    icon="mdi:heart-pulse",
                ),
            ),
        ]
    )


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
