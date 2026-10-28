"""Import ring-recorded history into the HA recorder.

The ring keeps logging heart rate (5-min slots) and activity (15-min bins)
while it is out of Bluetooth range. HA cannot attach historical timestamps to
new state-machine events, and Home Assistant exposes NO service for adding
historical states (``recorder.import_entity_history`` does not exist in any
released HA version), so we bulk-insert ``states`` rows directly through the
recorder's own DB session, running in the recorder's executor thread. Each
poll we diff the ring's day log against a persisted per-entity watermark and
import only the backlog, so the history graph shows the off-ring samples at
their true times.

Heart rate (``MEASUREMENT``) backfill is safe at both the raw-history and
statistics layers. The steps-family sensors are ``TOTAL_INCREASING``:
imported rows appear on the raw history graph but do not produce long-term
statistics (buckets for past ranges are already compacted) and can interact
with the total-increasing reset heuristic near live/import junctions, so that
backfill is gated behind an experimental option (see plans/hr-backfill-plan.md).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance, session_scope
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1

# Backfill kinds mapped to the sensor unique id they import into. The heart
# rate sensor historically registered with the bare entry id (see sensor.py);
# the other sensors follow the "<entry_id>_<key>" convention.
_KIND_UNIQUE_SUFFIX: dict[str, str | None] = {
    "heart_rate": None,
    "steps": "steps",
    "distance": "distance",
    "calories": "calories",
}

# Index of each sport metric within an api.py ``sport_bins`` row
# (timestamp, steps, distance, calories_kcal).
_SPORT_INDEX = {"steps": 1, "distance": 2, "calories": 3}


def _cumulative_sport_series(
    bins: list[tuple[datetime, int, int, float]], index: int
) -> list[tuple[datetime, int]]:
    """Convert per-bin activity data into a cumulative day-total time series.

    Matches the live sensors' ``TOTAL_INCREASING`` day-total semantics (calories
    rounded to whole kcal like the live sensor). Bins that do not change the
    running total emit no row; the previous state implicitly continues. The
    running total resets at each UTC day boundary, so a series that mixes
    yesterday's and today's bins (multi-day backfill) still reads as per-day
    totals.
    """
    running = 0.0
    series: list[tuple[datetime, int]] = []
    last_value: int | None = None
    last_day = None
    for row in bins:
        day = row[0].date()
        if day != last_day:
            running = 0.0
            last_value = None
            last_day = day
        running += row[index]
        value = round(running)
        if value != last_value:
            series.append((row[0], value))
            last_value = value
    return series


class BackfillManager:
    """Import off-ring ring history into the recorder, deduped by watermark."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Bind to a config entry; watermarks load lazily on first sync."""
        self._hass = hass
        self._entry_id = entry_id
        # Store signature is Store(hass, version, key).
        self._store: Store[dict[str, str]] = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}_backfill_{entry_id}",
        )
        self._watermarks: dict[str, datetime] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._gates_logged = False

    async def async_sync(
        self,
        data: dict[str, Any],
        *,
        hr_enabled: bool,
        steps_enabled: bool,
    ) -> None:
        """Import any backlog from this poll's ring logs for enabled metrics."""
        # DIAGNOSTIC (one-shot): proves the new backfill.py is deployed and
        # shows which gate would silently disable imports.
        if not self._gates_logged:
            self._gates_logged = True
            # WARNING on purpose: this user's HA log only keeps WARNING+,
            # and INFO diagnostics are invisible there.
            LOGGER.warning(
                "Colmi backfill: sync called (hr_enabled=%s steps_enabled=%s "
                "hr_entries=%d sport_bins=%d)",
                hr_enabled,
                steps_enabled,
                len(data.get("hr_entries") or []),
                len(data.get("sport_bins") or []),
            )
        if not hr_enabled and not steps_enabled:
            LOGGER.warning("Colmi backfill: both metrics disabled; skipping")
            return
        try:
            recorder = get_instance(self._hass)
        except (KeyError, RuntimeError):
            # Recorder not set up (hass.data key missing) or unavailable:
            # nowhere to import history into. Watermarks stay put so the
            # backlog imports once the recorder is up.
            LOGGER.warning("Colmi backfill: recorder unavailable; skipping")
            return
        hr_entries = data.get("hr_entries") or []
        sport_bins = data.get("sport_bins") or []
        if not hr_entries and not sport_bins:
            # Nothing to import this poll; watermarks stay as they are so the
            # first non-empty batch still imports the whole backlog.
            # WARNING (diagnostic): if this repeats all day, the ring log
            # queries are returning empty and backfill can never see data.
            LOGGER.warning(
                "Colmi backfill: no hr_entries/sport_bins in poll data; nothing "
                "to import (ring log query returned empty)"
            )
            return
        async with self._lock:
            await self._async_ensure_loaded()
            changed = False
            if hr_enabled:
                changed |= await self._async_sync_kind(
                    recorder, "heart_rate", hr_entries
                )
            if steps_enabled and sport_bins:
                for kind, index in _SPORT_INDEX.items():
                    changed |= await self._async_sync_kind(
                        recorder,
                        kind,
                        _cumulative_sport_series(sport_bins, index),
                    )
            if changed:
                await self._store.async_save(
                    {
                        kind: ts.isoformat()
                        for kind, ts in self._watermarks.items()
                    }
                )

    async def async_pending_before(
        self, day_start: datetime, *, hr_enabled: bool, steps_enabled: bool
    ) -> bool:
        """True when an enabled metric's watermark still predates day_start.

        The coordinator uses this to ask the API client for yesterday's
        UTC-day ring log only while some enabled metric has a backlog older
        than today's start: the ring keys its logs to UTC days, so a western
        user's daytime samples land mostly in the *previous* UTC day and are
        unreachable from today's log alone. Once every enabled watermark
        catches up to today, the extra day of BLE traffic stops on its own.
        """
        if not hr_enabled and not steps_enabled:
            return False
        await self._async_ensure_loaded()
        kinds = ["heart_rate"] if hr_enabled else []
        if steps_enabled:
            kinds.extend(_SPORT_INDEX)
        return any(
            self._watermarks.get(kind) is None
            or self._watermarks[kind] < day_start
            for kind in kinds
        )

    async def _async_ensure_loaded(self) -> None:
        """Load persisted watermarks once (no imports until we know them)."""
        if self._loaded:
            return
        saved = await self._store.async_load() or {}
        self._watermarks = {
            kind: datetime.fromisoformat(ts) for kind, ts in saved.items()
        }
        self._loaded = True

    async def _async_sync_kind(
        self, recorder: Recorder, kind: str, series: list[tuple[datetime, Any]]
    ) -> bool:
        """Import one sensor's backlog; return True if its watermark advanced.

        The newest sample is left out: it is (or will be) the live sensor state
        and flows through the normal state machine. Once a newer sample
        supersedes it, the next poll imports it as part of the backlog.
        """
        watermark = self._watermarks.get(kind)
        filtered = [
            (ts, value)
            for ts, value in series
            if watermark is None or ts > watermark
        ]
        # The newest sample flows through the live sensor, so import everything
        # before it: each imported row runs from its sample time until the next
        # sample's time, the last one ending exactly where the live newest row
        # begins (no overlap with states the state machine already recorded).
        pending = filtered[:-1]
        if not pending:
            return False
        entity_id = self._async_entity_id(kind)
        if entity_id is None:
            # Entity not registered yet (e.g. first refresh before platform
            # setup); do not advance the watermark so the next poll retries.
            # WARNING (diagnostic): persistent lines here mean the entity
            # registry lookup is wrong, so backfill never imports.
            LOGGER.warning(
                "Colmi backfill: no sensor entity for %s yet; deferring %d rows",
                kind,
                len(pending),
            )
            return False
        # Row end time is the next sample's time (filtered[i + 1]) so imported
        # rows tile up to where the live newest row begins, without overlap.
        rows = [
            (ts, filtered[i + 1][0], str(value))
            for i, (ts, value) in enumerate(pending)
        ]
        try:
            inserted = await self._async_import_history(recorder, entity_id, rows)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Colmi ring %s history backfill failed", kind)
            return False
        self._watermarks[kind] = pending[-1][0]
        # WARNING (diagnostic): these lines prove the import path is running;
        # if they appear but the Lovelace graph stays empty, the rows are
        # landing with wrong timestamps (timezone handling suspect).
        LOGGER.warning(
            "Colmi ring backfilled %d %s history rows (%s .. %s)",
            inserted,
            kind,
            pending[0][0],
            pending[-1][0],
        )
        return True

    async def _async_import_history(
        self,
        recorder: Recorder,
        entity_id: str,
        rows: list[tuple[datetime, datetime, str]],
    ) -> int:
        """Bulk-insert ``states`` rows for one entity, in the recorder thread.

        Runs entirely inside the recorder's own executor with its own session,
        mirroring how the recorder commits states itself: resolve (or create)
        the entity's ``states_meta`` row, chain ``old_state_id`` from the last
        state recorded before the backlog, then insert the imported rows in
        chronological order. Returns the number of rows inserted.
        """
        from sqlalchemy import select  # noqa: PLC0415

        from homeassistant.components.recorder.db_schema import (  # noqa: PLC0415
            States,
            StatesMeta,
        )

        def _import() -> int:
            def _ts(value: datetime) -> float:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=dt_util.get_default_time_zone())
                return value.timestamp()

            with session_scope(session=recorder.get_session()) as session:
                metadata_id = session.execute(
                    select(StatesMeta.metadata_id).where(
                        StatesMeta.entity_id == entity_id
                    )
                ).scalar_one_or_none()
                if metadata_id is None:
                    meta = StatesMeta(entity_id=entity_id)
                    session.add(meta)
                    session.flush()
                    metadata_id = meta.metadata_id

                # Continue the old_state_id chain from whatever the state
                # machine recorded just before our first imported row.
                old_state_id = session.execute(
                    select(States.state_id)
                    .where(
                        States.metadata_id == metadata_id,
                        States.last_updated_ts < _ts(rows[0][0]),
                    )
                    .order_by(States.last_updated_ts.desc())
                    .limit(1)
                ).scalar_one_or_none()

                inserted = 0
                for start, _end, state in rows:
                    stamp = _ts(start)
                    # entity_id/attributes are legacy unused columns since
                    # schema 31+; the entity is identified via metadata_id
                    # and attributes_id=None means "no attributes".
                    row = States(
                        state=state,
                        last_changed_ts=stamp,
                        last_updated_ts=stamp,
                        last_reported_ts=stamp,
                        old_state_id=old_state_id,
                        attributes_id=None,
                        metadata_id=metadata_id,
                    )
                    session.add(row)
                    session.flush()  # populates row.state_id
                    old_state_id = row.state_id  # chain within the batch
                    inserted += 1
                return inserted

        return await recorder.async_add_executor_job(_import)

    def _async_entity_id(self, kind: str) -> str | None:
        """Resolve the sensor entity id registered for one backfill kind."""
        suffix = _KIND_UNIQUE_SUFFIX[kind]
        unique_id = (
            self._entry_id if suffix is None else f"{self._entry_id}_{suffix}"
        )
        return er.async_get(self._hass).async_get_entity_id(
            "sensor", DOMAIN, unique_id
        )
