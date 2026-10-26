"""DataUpdateCoordinator for colmi_ring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ColmiRingApiClientAuthenticationError,
    ColmiRingApiClientError,
)
from .colmi_codec.hr_settings import HeartRateLogSettings
from .const import CONF_HR_STALE_POLLS, LOGGER

if TYPE_CHECKING:
    from .data import ColmiRingConfigEntry


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class ColmiRingDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: ColmiRingConfigEntry

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Track the latest on-demand readings across normal polls."""
        super().__init__(*args, **kwargs)
        # {"spo2": int | None, "spo2_timestamp": datetime | None} or None
        self.spo2_reading: dict[str, Any] | None = None
        # {"heart_rate": int | None, "heart_rate_timestamp": datetime | None}
        # A live real-time reading, kept only while it is fresher than the
        # ring's logged HR sample (see _async_update_data precedence).
        self.hr_live: dict[str, Any] | None = None

    @staticmethod
    def _prefer_newer_hr(data: dict[str, Any], live: dict[str, Any]) -> bool:
        """True if the live reading is usable and at least as new as logged."""
        live_hr = live.get("heart_rate")
        if live_hr is None:
            return False
        live_ts = live.get("heart_rate_timestamp")
        logged_ts = data.get("heart_rate_timestamp")
        if live_ts is None:
            return logged_ts is None
        if logged_ts is None:
            return True
        return live_ts >= logged_ts

    async def _async_update_data(self) -> Any:
        """Update data via library (merging persisted on-demand readings)."""
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
        # Heart rate precedence: an on-demand live reading wins only while it is
        # fresher than the ring's own logged sample, so the 5-min log poll
        # naturally overtakes once the ring records a newer HR sample.
        if self.hr_live is not None:
            if self._prefer_newer_hr(data, self.hr_live):
                data["heart_rate"] = self.hr_live.get("heart_rate")
                data["heart_rate_timestamp"] = self.hr_live.get(
                    "heart_rate_timestamp"
                )
            else:
                self.hr_live = None
        if self._heart_rate_is_stale(data):
            # The newest sample is old enough that the ring is almost certainly
            # off the finger; report "unknown" instead of a stale value.
            if data.get("heart_rate") is not None:
                LOGGER.debug(
                    "Colmi ring heart-rate sample is stale; clearing heart rate"
                )
            data["heart_rate"] = None
            data["heart_rate_timestamp"] = None
            self.hr_live = None
        return data

    def _heart_rate_is_stale(self, data: dict[str, Any]) -> bool:
        """True if the newest HR sample is older than the staleness threshold.

        The threshold is ``stale_polls * poll_interval + ring sample interval +
        5 min slack`` so a *worn* ring (whose newest sample is naturally one
        logging interval old) is never falsely cleared: with the default of 0
        extra polls, the sensor still goes unknown within about one logging
        interval of the ring leaving the finger, instead of waiting until
        midnight like the ring's daily log would.
        """
        timestamp = data.get("heart_rate_timestamp")
        if data.get("heart_rate") is None or not isinstance(timestamp, datetime):
            return False
        try:
            stale_polls = int(
                self.config_entry.options.get(CONF_HR_STALE_POLLS, 0)
            )
        except (TypeError, ValueError):
            stale_polls = 0
        poll_minutes = (
            self.update_interval.total_seconds() / 60
            if self.update_interval is not None
            else 5.0
        )
        settings = data.get("hr_settings")
        interval_minutes = (
            settings.interval
            if isinstance(settings, HeartRateLogSettings)
            else 15
        )
        threshold = timedelta(
            minutes=stale_polls * poll_minutes + interval_minutes + 5
        )
        return datetime.now(tz=timezone.utc) - timestamp > threshold

    def apply_spo2(self, reading: dict[str, Any]) -> None:
        """Store a fresh SpO2 reading and push it to entities immediately."""
        self.spo2_reading = reading
        data = dict(self.data) if self.data else {}
        data["spo2"] = reading.get("spo2")
        data["spo2_timestamp"] = reading.get("spo2_timestamp")
        self.async_set_updated_data(data)

    def apply_heart_rate(self, reading: dict[str, Any]) -> None:
        """Store a fresh live HR reading and push it to entities immediately."""
        self.hr_live = reading
        data = dict(self.data) if self.data else {}
        data["heart_rate"] = reading.get("heart_rate")
        data["heart_rate_timestamp"] = reading.get("heart_rate_timestamp")
        self.async_set_updated_data(data)
