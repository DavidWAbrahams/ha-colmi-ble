"""DataUpdateCoordinator for colmi_ring."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ColmiRingApiClientAuthenticationError,
    ColmiRingApiClientBusyError,
    ColmiRingApiClientError,
)
from .colmi_codec import date_utils
from .colmi_codec.hr_settings import HeartRateLogSettings
from .const import (
    CONF_BACKFILL_ON_RECONNECT,
    CONF_DEVICE_FAMILY,
    CONF_HR_STALE_POLLS,
    CONF_POLL_MAX_INTERVAL,
    CONF_YCBT_MONITOR_INTERVAL,
    DEFAULT_POLL_MAX_INTERVAL,
    DEFAULT_YCBT_MONITOR_INTERVAL,
    FAMILY_YCBT,
    LOGGER,
    clamp_ycbt_monitor_interval,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .backfill import BackfillManager
    from .data import ColmiRingConfigEntry
    from .motion_manager import MotionManager

# Returned by _async_try_poll when the ring refused the poll because another
# session holds its single link. Distinct from None (every attempt failed):
# a busy refusal is not a connection fault and must not count towards the
# consecutive-failure budget.
_POLL_BUSY = object()


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class ColmiRingDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: ColmiRingConfigEntry

    # Communication failures inside one poll cycle: attempts and backoff.
    # The link is opened on demand for each poll (see SharedLink), so a
    # failure is usually transient: HA's scanner missed a few of the ring's
    # ~1 s advertisements, or one GATT connect hiccup. Each of those clears
    # within seconds, while the next scheduled poll is 5-30 minutes away -
    # so re-failing straight to UpdateFailed would strand the entities as
    # Unavailable for minutes over a glitch that lasts a second.
    _POLL_ATTEMPTS = 3
    _POLL_RETRY_BACKOFF_S = (2.0, 5.0)
    # Whole failed poll cycles tolerated, on top of the in-cycle retries,
    # before the entities are allowed to go Unavailable. This keeps a brief
    # walk out of range invisible while a genuinely gone ring still reports
    # as unavailable (rather than silently freezing) within a few intervals.
    _TOLERATED_FAILED_CYCLES = 2

    def __init__(
        self, *args: Any, backfill: BackfillManager | None = None, **kwargs: Any
    ) -> None:
        """Track the latest on-demand readings across normal polls."""
        super().__init__(*args, **kwargs)
        # Imports off-ring HR/activity samples into the recorder each poll.
        self.backfill = backfill
        # Set by async_setup_entry once the manager exists (it needs us to be
        # constructed first). While its ``holds_link`` is set we must not open
        # a connection of our own: the ring serves one central at a time, and
        # either of its two sessions (RAW stream, wave listener) owns that link.
        self.motion: MotionManager | None = None
        # keys spo2 (int | None) and spo2_timestamp (datetime | None), or None
        self.spo2_reading: dict[str, Any] | None = None
        # keys heart_rate (int | None) and heart_rate_timestamp (datetime)
        # A live real-time reading, kept only while it is fresher than the
        # ring's logged HR sample (see _async_update_data precedence).
        self.hr_live: dict[str, Any] | None = None
        # {"hrv": int | None, "hrv_timestamp": datetime | None} or None.
        # The latest on-demand HRV reading from the Read HRV button; like
        # SpO2 it persists across polls (the Colmi poll data has no hrv key
        # of its own, so this overlay is the sensor's only Colmi source).
        self.hrv_reading: dict[str, Any] | None = None
        # Name of the on-demand measurement in progress ("heart rate", "SpO2",
        # ...), or None. The optical sensor takes one measurement at a time,
        # so the Read buttons and the Motion Stream switch grey out while this
        # is set (see ColmiRingEntity._watch_ring_busy).
        self.ring_busy: str | None = None
        self._busy_listeners: list[Callable[[str | None], None]] = []
        # Consecutive poll cycles in which every attempt failed; see
        # _TOLERATED_FAILED_CYCLES.
        self._failed_cycles = 0
        # Wall-clock UTC time of the most recent sign of life from the ring:
        # an advertisement seen by HA's scanner, or any GATT notification we
        # received. Pushed (not polled): see async_note_ring_seen.
        self.last_seen: datetime | None = None
        self._seen_listeners: list[Callable[[datetime], None]] = []

    # Throttle for the seen signal: the timestamp is floored to whole
    # seconds, so every advertisement/notification within the same second is
    # a no-op. The ring advertises about once per second and a live stream
    # notifies faster, so this bounds state writes (and thus recorder rows
    # for the entity) to roughly one per second without any timer.
    _SEEN_THROTTLE_S = 1.0

    @callback
    def async_note_ring_seen(self) -> None:
        """
        Record that the ring was just heard from; push if the second moved.

        Called from HA's bluetooth advertisement callback and from the GATT
        notification handlers of both protocol clients. Cheap and idempotent
        within one second: the floored timestamp doubles as the throttle, so
        a burst of notifications re-notifies the listeners at most once per
        second.
        """
        seen = datetime.now(tz=UTC).replace(microsecond=0)
        if self.last_seen is not None and (
            seen - self.last_seen < timedelta(seconds=self._SEEN_THROTTLE_S)
        ):
            return
        self.last_seen = seen
        for listener in list(self._seen_listeners):
            try:
                listener(seen)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring seen-listener failed")

    def add_seen_listener(
        self, listener: Callable[[datetime], None]
    ) -> Callable[[], None]:
        """Learn each (throttled) moment the ring is heard from; unsub returned."""
        self._seen_listeners.append(listener)
        return lambda: self._remove_seen_listener(listener)

    def _remove_seen_listener(self, listener: Callable[[datetime], None]) -> None:
        if listener in self._seen_listeners:
            self._seen_listeners.remove(listener)

    def add_busy_listener(
        self, listener: Callable[[str | None], None]
    ) -> Callable[[], None]:
        """Learn when a measurement starts or stops; returns an unsubscribe."""
        self._busy_listeners.append(listener)
        return lambda: self._remove_busy_listener(listener)

    def _remove_busy_listener(self, listener: Callable[[str | None], None]) -> None:
        if listener in self._busy_listeners:
            self._busy_listeners.remove(listener)

    @callback
    def async_set_ring_busy(self, running: str | None) -> None:
        """Announce a measurement starting (its name) or finishing (None)."""
        if self.ring_busy == running:
            return
        self.ring_busy = running
        for listener in list(self._busy_listeners):
            try:
                listener(running)
            except Exception:  # noqa: BLE001
                # One bad listener must not strand the rest.
                LOGGER.exception("Colmi ring busy-state listener failed")

    @property
    def _is_ycbt(self) -> bool:
        """True when this entry's ring speaks the Yucheng YCBT protocol."""
        return self.config_entry.data.get(CONF_DEVICE_FAMILY) == FAMILY_YCBT

    @property
    def _ycbt_monitor_interval(self) -> int:
        """
        The Yucheng sampling cadence this entry's options ask for.

        The family has no interval read opcode, so the option value is the
        source of truth: the number entity writes it, and the client's init
        burst re-applies it to the ring on every connect.
        """
        return clamp_ycbt_monitor_interval(
            self.config_entry.options.get(
                CONF_YCBT_MONITOR_INTERVAL, DEFAULT_YCBT_MONITOR_INTERVAL
            )
        )

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
        """
        (hr_enabled, steps_enabled, include_previous_day) for this poll.

        A single option now drives both metrics: when backfill is on, the
        ring's whole out-of-range backlog (heart rate and steps alike) is
        recovered; when off, nothing is imported.

        Ask the API client for yesterday's UTC-day ring log until the
        backfill manager records that day as fetched-and-imported: the ring
        keys its logs to UTC days, so e.g. a UTC-7 user's workday samples
        sit mostly in the previous UTC day's log - and those rows sort
        *below* the (today-based) watermark, so the watermark alone can
        never surface them.
        """
        enabled = bool(self.config_entry.options.get(CONF_BACKFILL_ON_RECONNECT, True))
        hr_enabled = steps_enabled = enabled
        include_previous_day = False
        if self.backfill is not None:
            prev_day = (
                (date_utils.start_of_day(date_utils.now()) - timedelta(days=1))
                .date()
                .isoformat()
            )
            include_previous_day = await self.backfill.async_needs_day(
                prev_day, hr_enabled=hr_enabled, steps_enabled=steps_enabled
            )
        return hr_enabled, steps_enabled, include_previous_day

    async def _async_try_poll(self, *, include_previous_day: bool) -> Any:
        """
        Poll once, retrying transient BLE failures inside this cycle.

        Returns the data dict on success, ``_POLL_BUSY`` if the ring's single
        link is held by another session, or ``None`` if every attempt failed
        with a communication error (the caller decides how many failed cycles
        to tolerate before reporting the device unavailable).

        The retry lives here rather than in the client because only the
        coordinator knows the cost of failing: the entities go Unavailable
        for a whole poll interval, so anything recoverable in a few seconds
        is worth retrying now instead of at the next scheduled refresh.
        """
        for attempt in range(self._POLL_ATTEMPTS):
            if attempt:
                await asyncio.sleep(
                    self._POLL_RETRY_BACKOFF_S[
                        min(attempt - 1, len(self._POLL_RETRY_BACKOFF_S) - 1)
                    ]
                )
            try:
                return await self.config_entry.runtime_data.client.async_get_data(
                    include_previous_day=include_previous_day
                )
            except ColmiRingApiClientBusyError:
                return _POLL_BUSY
            except ColmiRingApiClientAuthenticationError as exception:
                raise ConfigEntryAuthFailed(exception) from exception
            except ColmiRingApiClientError as exception:
                LOGGER.debug(
                    "Colmi ring poll attempt %s/%s failed: %s",
                    attempt + 1,
                    self._POLL_ATTEMPTS,
                    exception,
                )
            except Exception as exception:
                # A bug, not radio noise: do not retry it, and keep the
                # traceback in the log for diagnosis.
                LOGGER.exception("Unexpected error updating Colmi ring")
                raise UpdateFailed(exception) from exception
        # Every attempt failed; the caller decides whether this cycle is the
        # one that finally reports the device unavailable.
        return None

    async def _async_update_data(self) -> Any:
        """Update data via library (merging persisted on-demand readings)."""
        if self.motion is not None and self.motion.holds_link:
            # The RAW motion stream holds the ring's single GATT link
            # exclusively. Rather than fight it for the connection, keep the
            # last known values and resume as soon as it closes (the manager
            # refreshes us). History backfill pauses too, which is harmless: it
            # is idempotent and picks the backlog up on the next real poll.
            # Wave detection is deliberately absent from this gate: it shares
            # the link, and its commands only queue briefly behind the link's
            # transaction lock.
            return self.data
        (
            hr_enabled,
            steps_enabled,
            include_previous_day,
        ) = await self._async_backfill_flags()
        data = await self._async_try_poll(include_previous_day=include_previous_day)
        if data is _POLL_BUSY:
            # The link was busy for longer than the poll's short wait: the
            # motion stream claimed it between the gate above and here
            # (toggling it on races a poll already in flight), or a wave
            # arm/disarm write is just finishing. That is not a fault to
            # report, so keep the last known values and let the next poll
            # proceed normally.
            LOGGER.debug(
                "Colmi ring poll skipped: the ring is busy with another session"
            )
            return self.data
        if data is None:
            # Every attempt in this cycle failed with a communication error.
            # Tolerate a few consecutive failed cycles (the ring briefly out
            # of range) before letting the entities go Unavailable, so a
            # second-or-two of BLE noise does not blank minutes of history.
            self._failed_cycles += 1
            # No successful poll yet (first refresh during setup): keep the
            # original behaviour of failing loudly so HA retries the config
            # entry instead of setting it up with no data.
            if self.data is None or self._failed_cycles > self._TOLERATED_FAILED_CYCLES:
                msg = (
                    "Colmi ring unreachable for"
                    f" {self._failed_cycles} consecutive poll cycles"
                )
                raise UpdateFailed(msg)
            LOGGER.debug(
                "Colmi ring poll cycle failed (%s/%s tolerated before"
                " reporting unavailable)",
                self._failed_cycles,
                self._TOLERATED_FAILED_CYCLES,
            )
            return self.data
        self._failed_cycles = 0
        self._apply_spo2_reading(data)
        self._apply_live_heart_rate(data)
        self._apply_hrv_reading(data)
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
        """
        Overlay the on-demand live heart rate while it is the fresher sample.

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

    def _apply_hrv_reading(self, data: dict[str, Any]) -> None:
        """
        Overlay the persisted on-demand HRV reading, if any.

        Yucheng polls already carry their own hrv keys (the client persists
        history-derived HRV itself), and the coordinator's ``hrv_reading``
        stays unset for those entries, so ``setdefault`` leaves them alone.
        """
        if self.hrv_reading is not None:
            data["hrv"] = self.hrv_reading.get("hrv")
            data["hrv_timestamp"] = self.hrv_reading.get("hrv_timestamp")
        else:
            data.setdefault("hrv", None)
            data.setdefault("hrv_timestamp", None)

    # Floor for the adaptive poll interval (Colmi family). The ring's HR log
    # is a 5-min slot grid, so polling faster than that can never reveal a
    # new sample. Yucheng records carry minute-granular timestamps and their
    # sampling rate is ours to choose, so that family may poll faster.
    _MIN_POLL_MINUTES = 5
    _YCBT_MIN_POLL_MINUTES = 1

    def _apply_poll_interval(self, data: dict[str, Any]) -> None:
        """
        Match the poll cadence to the ring's sampling interval, capped.

        The ring only records a new HR sample once per its configured
        sampling interval, so polling faster just re-reads an unchanged log
        and wastes battery. We therefore poll at ``min(ring interval,
        configured max)``, floored at the 5-minute log grid. When the ring
        did not answer the settings query (Yucheng rings never do; a Colmi
        ring that dropped the query keeps its current interval rather than
        snapping to a default). ``DataUpdateCoordinator`` reads
        ``update_interval`` when scheduling the next refresh, so assigning
        it here retunes subsequent polls.
        """
        settings = data.get("hr_settings")
        if self._is_ycbt:
            # Yucheng family: no settings reply; the option the integration
            # pushes to the ring is the sampling interval.
            sample_minutes = self._ycbt_monitor_interval
        elif not isinstance(settings, HeartRateLogSettings):
            return
        else:
            sample_minutes = settings.interval
        floor = self._YCBT_MIN_POLL_MINUTES if self._is_ycbt else self._MIN_POLL_MINUTES
        try:
            max_minutes = int(
                self.config_entry.options.get(
                    CONF_POLL_MAX_INTERVAL, DEFAULT_POLL_MAX_INTERVAL
                )
            )
        except (TypeError, ValueError):
            max_minutes = DEFAULT_POLL_MAX_INTERVAL
        minutes = max(
            floor,
            min(sample_minutes, max_minutes),
        )
        new_interval = timedelta(minutes=minutes)
        if self.update_interval != new_interval:
            LOGGER.debug(
                "Colmi ring poll interval set to %s (sampling %s min, max %s min)",
                new_interval,
                sample_minutes,
                max_minutes,
            )
            self.update_interval = new_interval

    def _heart_rate_is_stale(self, data: dict[str, Any]) -> bool:
        """
        True if the newest HR sample is older than the staleness threshold.

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
            stale_polls = int(self.config_entry.options.get(CONF_HR_STALE_POLLS, 3))
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
            # Yucheng rings have no settings reply; Colmi rings that failed the
            # settings query fall back to a conservative typical interval.
            else (self._ycbt_monitor_interval if self._is_ycbt else 15)
        )
        threshold = timedelta(minutes=stale_polls * poll_minutes + interval_minutes + 5)
        return datetime.now(tz=UTC) - timestamp > threshold

    def apply_spo2(self, reading: dict[str, Any]) -> None:
        """Store a fresh SpO2 reading and push it to entities immediately."""
        self.spo2_reading = reading
        data = dict(self.data) if self.data else {}
        data["spo2"] = reading.get("spo2")
        data["spo2_timestamp"] = reading.get("spo2_timestamp")
        self.async_set_updated_data(data)

    def apply_skin_temperature(self, reading: dict[str, Any]) -> None:
        """
        Push a fresh skin-temperature reading to entities immediately.

        Unlike SpO2 the Yucheng client persists the reading itself (it is
        what every later poll re-publishes), so the coordinator only has
        to make this press land now instead of at the next poll.
        """
        data = dict(self.data) if self.data else {}
        data["skin_temperature"] = reading.get("skin_temperature")
        data["skin_temperature_timestamp"] = reading.get("skin_temperature_timestamp")
        self.async_set_updated_data(data)

    def apply_hrv(self, reading: dict[str, Any]) -> None:
        """Store a fresh HRV reading and push it to entities immediately."""
        self.hrv_reading = reading
        data = dict(self.data) if self.data else {}
        data["hrv"] = reading.get("hrv")
        data["hrv_timestamp"] = reading.get("hrv_timestamp")
        self.async_set_updated_data(data)

    def apply_heart_rate(self, reading: dict[str, Any]) -> None:
        """Store a fresh live HR reading and push it to entities immediately."""
        self.hr_live = reading
        data = dict(self.data) if self.data else {}
        data["heart_rate"] = reading.get("heart_rate")
        data["heart_rate_timestamp"] = reading.get("heart_rate_timestamp")
        self.async_set_updated_data(data)

    def async_clear_heart_rate(self) -> None:
        """
        Drop the published heart rate and push "unknown" to entities now.

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
