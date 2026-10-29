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
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance, session_scope
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import HomeAssistant

# Persisted as {kind: {"wm": iso, "scanned": [iso days]}}. Older installs
# saved a bare {kind: iso}; the loader accepts both. The version is kept at 1
# on purpose: bumping it would make Store.async_load() drop the existing file
# (no MIGRATIONS), resetting watermarks and re-importing a day of duplicates.
# "scanned" records UTC days whose ring log was already fetched and imported,
# so rows that surface *below* the (mostly today-based) watermark from an
# older day are still trusted, and the previous-day fetch happens once per day.
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

# SQLite allows a single writer. Our import runs in the recorder's executor but
# is a *separate* transaction from the recorder's own commit loop, so on SQLite
# it regularly collides with it and the whole transaction is rejected with
# ``OperationalError: database is locked``. A rejected transaction rolls back
# (nothing is half-inserted, and the watermark is not advanced), so retrying it
# is safe. Three attempts wait 0.25 s then 0.5 s: about the time a recorder
# commit takes to land, and small enough that four sensors' worth of retries
# cannot stall the poll that triggered the sync.
_LOCK_RETRIES = 3
_LOCK_BACKOFF_S = 0.25

# How far back the attribute-repair pass reaches. It exists so imported rows
# carry the same unit_of_measurement/device_class as live ones, which the block
# processor only checks in the window it is currently compiling statistics for;
# an unbounded UPDATE rewrites the entity's *entire* history while holding the
# SQLite write lock, which is what loses the race against a busy recorder.
_REPAIR_WINDOW_S = 2 * 86400

# How writer contention surfaces, per recorder backend: SQLite says ``database
# is locked`` (SQLITE_BUSY) or ``database table is locked``, MariaDB reports a
# deadlock, PostgreSQL a serialization failure. SQLAlchemy wraps the DB-API
# error, so the text is matched across the whole exception chain instead of by
# exception class: importing ``sqlalchemy.exc`` here would couple this module to
# the recorder's engine at import time.
_DB_BUSY_MARKERS = (
    "database is locked",
    "database table is locked",
    "deadlock found",
    "could not serialize access",
)


def _db_is_busy(err: BaseException) -> bool:
    """True if ``err`` (or anything it wraps) is SQLite writer contention."""
    seen: set[int] = set()
    pending: list[BaseException] = [err]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if any(marker in str(current).lower() for marker in _DB_BUSY_MARKERS):
            return True
        linked = [current.__cause__, current.__context__]
        linked.append(getattr(current, "orig", None))
        pending.extend(item for item in linked if isinstance(item, BaseException))
    return False


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
        # ISO dates (YYYY-MM-DD) of UTC days whose ring log was already
        # fetched and imported (see async_sync retry_day).
        self._scanned: set[str] = set()
        self._loaded = False
        self._lock = asyncio.Lock()
        self._gates_logged = False

    async def async_sync(
        self,
        data: dict[str, Any],
        *,
        hr_enabled: bool,
        steps_enabled: bool,
        retry_day: str | None = None,
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
            kind_results: dict[str, tuple[bool, bool]] = {}
            if hr_enabled:
                kind_results["heart_rate"] = await self._async_sync_kind(
                    recorder,
                    "heart_rate",
                    hr_entries,
                    retry_day=retry_day,
                )
            if steps_enabled and sport_bins:
                for kind, index in _SPORT_INDEX.items():
                    kind_results[kind] = await self._async_sync_kind(
                        recorder,
                        kind,
                        _cumulative_sport_series(sport_bins, index),
                        retry_day=retry_day,
                    )
            changed = any(ok for ok, _ in kind_results.values())
            if retry_day is not None:
                deferred = [
                    kind
                    for kind, (imported, had_day) in kind_results.items()
                    if had_day and not imported
                ]
                if deferred:
                    # Import failed or was deferred (entity not registered
                    # yet): keep re-fetching the day so its samples are not
                    # lost below the today-based watermark.
                    LOGGER.warning(
                        "Colmi backfill: %s log fetched but import deferred "
                        "for %s; will re-fetch next poll",
                        retry_day,
                        deferred,
                    )
                else:
                    # Fetched and handled (imported, or the day was empty):
                    # never fetch this day again. The watermark alone cannot
                    # decide this - it sits at *today's* latest sample, above
                    # the whole older day, which previously let its rows be
                    # filtered out as "already imported" though they never
                    # were.
                    self._scanned.add(retry_day)
                    # Bound growth: only recent days can ever be re-asked.
                    cutoff = (
                        datetime.now(tz=timezone.utc) - timedelta(days=8)
                    ).date().isoformat()
                    self._scanned = {d for d in self._scanned if d >= cutoff}
                    changed = True
            if changed:
                await self._store.async_save(
                    {
                        kind: {"wm": ts.isoformat(), "scanned": self._scanned}
                        for kind, ts in self._watermarks.items()
                    }
                )

    async def async_needs_day(
        self, day: str, *, hr_enabled: bool, steps_enabled: bool
    ) -> bool:
        """True until ``day``'s ring log has been fetched and imported once.

        The coordinator uses this to ask the API client for the previous
        UTC-day ring log: the ring keys its logs to UTC days, so a western
        user's daytime samples land mostly in the *previous* UTC day and are
        unreachable from today's log alone - and those rows sort below the
        (today-based) watermark, so the watermark can never surface them.
        The day is marked scanned in async_sync once handled; if the import
        was deferred it stays unscanned and gets re-fetched next poll.
        """
        if not hr_enabled and not steps_enabled:
            return False
        await self._async_ensure_loaded()
        return day not in self._scanned

    async def _async_ensure_loaded(self) -> None:
        """Load persisted watermarks once (no imports until we know them)."""
        if self._loaded:
            return
        saved = await self._store.async_load() or {}
        watermarks: dict[str, datetime] = {}
        for kind, raw in saved.items():
            if isinstance(raw, str):  # v1: bare watermark timestamp
                watermarks[kind] = datetime.fromisoformat(raw)
            elif isinstance(raw, dict) and raw.get("wm"):
                watermarks[kind] = datetime.fromisoformat(raw["wm"])
                self._scanned.update(raw.get("scanned") or [])
        self._watermarks = watermarks
        self._loaded = True

    async def _async_sync_kind(
        self,
        recorder: Recorder,
        kind: str,
        series: list[tuple[datetime, Any]],
        retry_day: str | None = None,
    ) -> tuple[bool, bool]:
        """Import one sensor's backlog.

        Returns ``(imported, had_retry_day_rows)``: whether any rows were
        inserted, and whether the raw series carried rows for the freshly
        fetched earlier day (so the caller only treats a failed import of
        that day as a reason to re-fetch).

        The newest sample is left out: it is (or will be) the live sensor state
        and flows through the normal state machine. Once a newer sample
        supersedes it, the next poll imports it as part of the backlog.
        """
        had_day = retry_day is not None and any(
            ts.date().isoformat() == retry_day for ts, _ in series
        )
        watermark = self._watermarks.get(kind)
        # Drop whole UTC days already fetched-and-imported. The watermark
        # alone cannot be trusted for older days: it sits at the newest
        # imported sample (i.e. *today's*), so a previously unreachable
        # earlier day's rows all sort below it and would be discarded as
        # "already imported" when they never were.
        filtered = [
            (ts, value)
            for ts, value in series
            if ts.date().isoformat() not in self._scanned
        ]
        # Within (today's) not-yet-scanned rows, the watermark filters what the
        # state machine or an earlier import already recorded.
        if watermark is not None:
            filtered = [
                (ts, value)
                for ts, value in filtered
                if ts.date() != watermark.date() or ts > watermark
            ]
        # The newest sample flows through the live sensor, so import everything
        # before it: each imported row runs from its sample time until the next
        # sample's time, the last one ending exactly where the live newest row
        # begins (no overlap with states the state machine already recorded).
        pending = filtered[:-1]
        if not pending:
            return False, had_day
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
            return False, had_day
        # Row end time is the next sample's time (filtered[i + 1]) so imported
        # rows tile up to where the live newest row begins, without overlap.
        rows = [
            (ts, filtered[i + 1][0], str(value))
            for i, (ts, value) in enumerate(pending)
        ]
        try:
            inserted = await self._async_import_history(recorder, entity_id, rows)
        except Exception as err:  # noqa: BLE001
            if _db_is_busy(err):
                # The recorder's own writer had the database locked for longer
                # than our retries. Nothing was written and the watermark is
                # untouched, so the next poll imports the same backlog; the
                # backlog drains on its own and a busy database is not worth a
                # traceback every poll.
                LOGGER.debug(
                    "Colmi ring %s history backfill deferred: the recorder"
                    " database is busy (%s)",
                    kind,
                    err,
                )
            else:
                LOGGER.exception("Colmi ring %s history backfill failed", kind)
            return False, had_day
        new_wm = pending[-1][0]
        if watermark is None or new_wm > watermark:
            self._watermarks[kind] = new_wm
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
        return True, had_day

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
        from sqlalchemy import select, update  # noqa: PLC0415

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

                # Reuse the attributes blob from the entity's newest
                # state-machine row so imported states carry the same
                # unit_of_measurement/device_class; rows without attributes
                # make the block processor see a None unit and warn
                # "unit is changing", suppressing long-term statistics.
                attributes_id = session.execute(
                    select(States.attributes_id)
                    .where(
                        States.metadata_id == metadata_id,
                        States.attributes_id.is_not(None),
                    )
                    .order_by(States.last_updated_ts.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if attributes_id is not None:
                    # Repair rows an older import (before imported rows carried
                    # attributes) left attribute-less, but only inside the
                    # window statistics still look at: an unbounded UPDATE
                    # rewrites the entity's whole history while holding the
                    # SQLite write lock, which is exactly what loses the race
                    # with the recorder's own writer.
                    repair_cutoff = _ts(
                        datetime.now(tz=timezone.utc)
                        - timedelta(seconds=_REPAIR_WINDOW_S)
                    )
                    session.execute(
                        update(States)
                        .where(
                            States.metadata_id == metadata_id,
                            States.attributes_id.is_(None),
                            States.last_updated_ts >= repair_cutoff,
                        )
                        .values(attributes_id=attributes_id)
                    )

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
                        attributes_id=attributes_id,
                        metadata_id=metadata_id,
                    )
                    session.add(row)
                    session.flush()  # populates row.state_id
                    old_state_id = row.state_id  # chain within the batch
                    inserted += 1
                return inserted

        # Retry the whole transaction, not the failing statement: SQLite
        # rejects the entire transaction on writer contention and rolls it back
        # in full, so re-running it cannot leave half a batch behind nor
        # duplicate rows. Waiting happens in the event loop, not in the
        # recorder's thread.
        attempts_left = _LOCK_RETRIES
        while True:
            attempts_left -= 1
            try:
                return await recorder.async_add_executor_job(_import)
            except Exception as err:  # noqa: BLE001
                if not attempts_left or not _db_is_busy(err):
                    raise
                await asyncio.sleep(
                    _LOCK_BACKOFF_S * 2 ** (_LOCK_RETRIES - attempts_left - 1)
                )

    def _async_entity_id(self, kind: str) -> str | None:
        """Resolve the sensor entity id registered for one backfill kind."""
        suffix = _KIND_UNIQUE_SUFFIX[kind]
        unique_id = (
            self._entry_id if suffix is None else f"{self._entry_id}_{suffix}"
        )
        return er.async_get(self._hass).async_get_entity_id(
            "sensor", DOMAIN, unique_id
        )
