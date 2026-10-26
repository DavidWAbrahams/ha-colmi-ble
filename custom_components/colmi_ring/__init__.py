"""
Custom integration to integrate colmi_ring with Home Assistant.

For more details about this integration, please refer to
https://github.com/ludeeus/integration_blueprint
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .api import ColmiRingApiClient
from .const import DOMAIN, LOGGER
from .coordinator import ColmiRingDataUpdateCoordinator
from .data import ColmiRingData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import ColmiRingConfigEntry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
]


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColmiRingConfigEntry,
) -> bool:
    """Set up this integration using UI."""
    coordinator = ColmiRingDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
        # TODO: think about a good value here. This probably eats battery.
        update_interval=timedelta(minutes=5),
    )
    entry.runtime_data = ColmiRingData(
        client=ColmiRingApiClient(hass=hass, ring_address=entry.data[CONF_ADDRESS]),
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
    )

    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    # TODO: When code is stable, this could become coordinator.async_refresh() which ignores errors
    # from the first refresh.
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ColmiRingConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ColmiRingConfigEntry,
) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
