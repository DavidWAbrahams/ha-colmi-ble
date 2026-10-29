"""
Custom integration to integrate colmi_ring with Home Assistant.

For more details about this integration, please refer to
https://github.com/ludeeus/integration_blueprint
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.loader import async_get_loaded_integration

from .api import ColmiRingApiClient
from .backfill import BackfillManager
from .const import DOMAIN, LOGGER
from .coordinator import ColmiRingDataUpdateCoordinator
from .data import ColmiRingData
from .motion_manager import MotionManager

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import ColmiRingConfigEntry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    # Both are created only when the motion option is on; the platforms
    # themselves check, so nothing appears when it is off.
    Platform.SWITCH,
    Platform.EVENT,
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
        # Bootstrap interval only: after the first successful poll the
        # coordinator retunes itself to the ring's HR sampling interval,
        # capped by the poll_max_interval option (see _apply_poll_interval).
        update_interval=timedelta(minutes=5),
        backfill=BackfillManager(hass, entry.entry_id),
    )
    client = ColmiRingApiClient(hass=hass, ring_address=entry.data[CONF_ADDRESS])
    # Always constructed (it does no I/O until asked) because the coordinator
    # needs to be able to ask whether the stream is holding the connection even
    # in setups where the option is off. The switch and event entities, in
    # contrast, only appear when CONF_MOTION_ENABLED is on.
    motion = MotionManager(hass, entry, client, coordinator)
    coordinator.motion = motion
    entry.runtime_data = ColmiRingData(
        client=client,
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
        motion=motion,
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
    # Close a live stream before its entities disappear, and before the
    # coordinator stops: otherwise the stream task is left holding a connection
    # nobody owns any more.
    motion = entry.runtime_data.motion
    if motion is not None:
        await motion.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ColmiRingConfigEntry,
) -> None:
    """Reload config entry.

    Must NOT manually unload-then-setup: that runs async_setup_entry while
    the entry state is NOT_LOADED, so the coordinator's
    async_config_entry_first_refresh() refuses with ConfigEntryError
    ("Config entry is not loaded") and every entity is orphaned with
    "no longer being provided by the integration". Delegating to HA's own
    reload path keeps its state machine consistent, and it debounces when
    the options flow also triggers an automatic reload.
    """
    entry.async_schedule_reload(hass)
