"""DataUpdateCoordinator for colmi_ring."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ColmiRingApiClientAuthenticationError,
    ColmiRingApiClientError,
)
from .const import LOGGER

if TYPE_CHECKING:
    from .data import ColmiRingConfigEntry


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class ColmiRingDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: ColmiRingConfigEntry

    async def _async_update_data(self) -> Any:
        """Update data via library."""
        try:
            return await self.config_entry.runtime_data.client.async_get_data()
        except ColmiRingApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except ColmiRingApiClientError as exception:
            raise UpdateFailed(exception) from exception
        except Exception as exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error updating Colmi ring")
            raise UpdateFailed(exception) from exception
