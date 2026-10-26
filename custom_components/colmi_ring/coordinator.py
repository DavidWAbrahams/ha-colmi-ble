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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Track the latest on-demand SpO2 reading across normal polls."""
        super().__init__(*args, **kwargs)
        # {"spo2": int | None, "spo2_timestamp": datetime | None} or None
        self.spo2_reading: dict[str, Any] | None = None

    async def _async_update_data(self) -> Any:
        """Update data via library (merging the persisted on-demand SpO2)."""
        try:
            data = await self.config_entry.runtime_data.client.async_get_data()
        except ColmiRingApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except ColmiRingApiClientError as exception:
            raise UpdateFailed(exception) from exception
        except Exception as exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error updating Colmi ring")
            raise UpdateFailed(exception) from exception
        if self.spo2_reading is not None:
            data["spo2"] = self.spo2_reading.get("spo2")
            data["spo2_timestamp"] = self.spo2_reading.get("spo2_timestamp")
        else:
            data.setdefault("spo2", None)
            data.setdefault("spo2_timestamp", None)
        return data

    def apply_spo2(self, reading: dict[str, Any]) -> None:
        """Store a fresh SpO2 reading and push it to entities immediately."""
        self.spo2_reading = reading
        data = dict(self.data) if self.data else {}
        data["spo2"] = reading.get("spo2")
        data["spo2_timestamp"] = reading.get("spo2_timestamp")
        self.async_set_updated_data(data)
