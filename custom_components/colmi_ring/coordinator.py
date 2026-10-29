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
from .colmi_codec import date_utils
from .colmi_codec.hr_settings import HeartRateLogSettings
from .const import (
    CONF_HR_BACKFILL,
    CONF_HR_STALE_POLLS,
    CONF_POLL_MAX_INTERVAL,
    CONF_STEPS_BACKFILL,
    DEFAULT_POLL_MAX_INTERVAL,
    LOGGER,
)

if TYPE_CHECKING:
    from .backfill import BackfillManager
    from .data import ColmiRingConfigEntry
    from .motion_manager import MotionManager


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class ColmiRingDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: ColmiRingConfigEntry

    def __init__(
        self, *args: Any, backfill: BackfillManager | None = None, **kwargs: Any
    ) -> None:
        """Track the latest on-demand readings across normal polls."""
        super().__init__(*args, **kwargs)
        # Imports off-ring HR/activity samples into the recorder each poll.
        self.backfill = backfill
        # Set by async_setup_entry once the manager exists (it needs us to be
        # constructed first). While its ``is_streaming`` is set we must not open
        # a connection of our own: the ring serves one central at a time.
        self.motion: MotionManager | None = None
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

    async def _async_backfill_flags(self) -> tuple[bool, bool, bool]:
        """(hr_enabled, steps_enabled, include_previous_day) for this poll.

        Ask the API client for yesterday's UTC-day ring log until the
        backfill manager records that day as fetched-and-imported: the ring
        keys its logs to UTC days, so e.g. a UTC-7 user's workday samples
        sit mostly in the previous UTC day's log - and those rows sort
        *below* the (today-based) watermark, so the watermark alone can
        never surface them.
        """
        hr_enabled = bool(self.config_entry.options.get(CONF_HR_BACKFILL, True))
        steps_enabled = bool(
            self.config_entry.options.get(CONF_STEPS_BACKFILL, False)
        )
        include_previous_day = False
        if self.backfill is not None:
            prev_day = (
                date_utils.start_of_day(date_utils.now()) - timedelta(days=1)
            ).date().isoformat()
            include_previous_day = await self.backfill.async_needs_day(
                prev_day, hr_enabled=hr_enabled, steps_enabled=steps_enabled
            )
        return hr_enabled, steps_enabled, include_previous_day

    async def _async_update_data(self) -> Any:
        """Update data via library (merging persisted on-demand readings)."""
        if self.motion is not None and self.motion.is_streaming:
            # The RAW motion stream holds the ring's single GATT link. Rather
            # than fight it for the connection, keep the last known values and
            # resume as soon as the stream closes (the manager refreshes us).
            # History backfill pauses too, which is harmless: it is idempotent
            # and picks the backlog up on the next real poll.
            return self.data
        hr_enabled, steps_enabled, include_previous_day = (
            await self._async_backfill_flags()
        )
        try:
            data = (
                await self.config_entry.runtime_data.client.async_get_data(
                    include_previous_day=include_previous_day
                )
            )
        except ColmiRingApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except ColmiRingApiClientError as exception:
            raise UpdateFailed(exception) from exception
        except Exception as exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error updating Colmi ring")
            raise UpdateFailed(exception) from exception
        self._apply_spo2_reading(data)
        self._apply_live_heart_rate(data)
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
        # Independent of the live-state logic above: samples the ring recorded
        # while out of range belong in the history even when the live sensor is
        # currently stale/unknown.
        if self.backfill is not None:
            try:
                await self.backfill.async_sync(
                    data,
                    hr_enabled=hr_enabled,
                    steps_enabled=steps_enabled,
                    retry_day=data.get("prev_day"),
                )
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring history backfill errored")
        self._apply_poll_interval(data)
        return data

    def _apply_spo2_reading(self, data: dict[str, Any]) -> None:
        """Overlay the persisted on-demand SpO2 reading, if any."""
        if self.spo2_reading is not None:
            data["spo2"] = self.spo2_reading.get("spo2")
            data["spo2_timestamp"] = self.spo2_reading.get("spo2_timestamp")
        else:
            data.setdefault("spo2", None)
            data.setdefault("spo2_timestamp", None)

    def _apply_live_heart_rate(self, data: dict[str, Any]) -> None:
        """Overlay the on-demand live heart rate while it is the fresher sample.

        Heart rate precedence: a live reading wins only while it is fresher
        than the ring's own logged sample, so the 5-min log poll naturally
        overtakes once the ring records a newer HR sample.
        """
        if self.hr_live is None:
            return
        if self._prefer_newer_hr(data, self.hr_live):
            data["heart_rate"] = self.hr_live.get("heart_rate")
            data["heart_rate_timestamp"] = self.hr_live.get("heart_rate_timestamp")
        else:
            self.hr_live = None

    # Floor for the adaptive poll interval. The ring's HR log is a 5-min
    # slot grid, so polling faster than that can never reveal a new sample.
    _MIN_POLL_MINUTES = 5

    def _apply_poll_interval(self, data: dict[str, Any]) -> None:
        """Match the poll cadence to the ring's sampling interval, capped.

        The ring only records a new HR sample once per its configured
        sampling interval, so polling faster just re-reads an unchanged log
        and wastes battery. We therefore poll at ``min(ring interval,
        configured max)``, floored at the 5-minute log grid. When the ring
        did not answer the settings query we keep the current interval
        rather than snapping to a default. ``DataUpdateCoordinator`` reads
        ``update_interval`` when scheduling the next refresh, so assigning
        it here retunes subsequent polls.
        """
        settings = data.get("hr_settings")
        if not isinstance(settings, HeartRateLogSettings):
            return
        try:
            max_minutes = int(
                self.config_entry.options.get(
                    CONF_POLL_MAX_INTERVAL, DEFAULT_POLL_MAX_INTERVAL
                )
            )
        except (TypeError, ValueError):
            max_minutes = DEFAULT_POLL_MAX_INTERVAL
        minutes = max(
            self._MIN_POLL_MINUTES,
            min(settings.interval, max_minutes),
        )
        new_interval = timedelta(minutes=minutes)
        if self.update_interval != new_interval:
            LOGGER.debug(
                "Colmi ring poll interval set to %s (sampling %s min, max %s min)",
                new_interval,
                settings.interval,
                max_minutes,
            )
            self.update_interval = new_interval

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
                self.config_entry.options.get(CONF_HR_STALE_POLLS, 3)
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

    def async_clear_heart_rate(self) -> None:
        """Drop the published heart rate and push "unknown" to entities now.

        Called when the motion stream opens. Polling stops for the duration, so
        leaving the last value in place would present a frozen measurement as
        though it were live; the sensor recovers on the refresh that follows the
        stream closing.
        """
        if not self.data or self.data.get("heart_rate") is None:
            self.hr_live = None
            return
        self.hr_live = None
        data = dict(self.data)
        data["heart_rate"] = None
        data["heart_rate_timestamp"] = None
        self.async_set_updated_data(data)
