"""Import ring-recorded history into the HA recorder.

The ring keeps logging heart rate (5-min slots) and activity (15-min bins)
while it is out of Bluetooth range. HA cannot attach historical timestamps to
new state-machine events, but the recorder's ``import_entity_history`` service
(the same one HA's own restore/migration flows use) bulk-inserts ``states``
rows with arbitrary past timestamps. Each poll we diff the ring's day log
against a persisted per-entity watermark and import only the backlog, so the
history graph shows the off-ring samples at their true times.

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
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORAGE_VERSION = 1
RECORDER_DOMAIN = "recorder"
SERVICE_IMPORT_ENTITY_HISTORY = "import_entity_history"

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
    running total emit no row; the previous state implicitly continues.
    """
    running = 0.0
    series: list[tuple[datetime, int]] = []
    last_value: int | None = None
    for row in bins:
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
        self._store: Store[dict[str, str]] = Store(
            STORAGE_VERSION,
            f"{DOMAIN}_backfill_{entry_id}",
            hass,
        )
        self._watermarks: dict[str, datetime] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def async_sync(
        self,
        data: dict[str, Any],
        *,
        hr_enabled: bool,
        steps_enabled: bool,
    ) -> None:
        """Import any backlog from this poll's ring logs for enabled metrics."""
        if not hr_enabled and not steps_enabled:
            return
        recorder = self._hass.services.async_services().get(RECORDER_DOMAIN)
        if not recorder or SERVICE_IMPORT_ENTITY_HISTORY not in recorder:
            LOGGER.debug(
                "Colmi backfill: %s.%s unavailable; skipping",
                RECORDER_DOMAIN,
                SERVICE_IMPORT_ENTITY_HISTORY,
            )
            return
        hr_entries = data.get("hr_entries") or []
        sport_bins = data.get("sport_bins") or []
        if not hr_entries and not sport_bins:
            # Nothing to import this poll; watermarks stay as they are so the
            # first non-empty batch still imports the whole backlog.
            return
        async with self._lock:
            await self._async_ensure_loaded()
            changed = False
            if hr_enabled:
                changed |= await self._async_sync_kind("heart_rate", hr_entries)
            if steps_enabled and sport_bins:
                for kind, index in _SPORT_INDEX.items():
                    changed |= await self._async_sync_kind(
                        kind, _cumulative_sport_series(sport_bins, index)
                    )
            if changed:
                await self._store.async_save(
                    {
                        kind: ts.isoformat()
                        for kind, ts in self._watermarks.items()
                    }
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
        self, kind: str, series: list[tuple[datetime, Any]]
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
            LOGGER.debug("Colmi backfill: no entity for %s yet", kind)
            return False
        history = [
            {
                "state": str(value),
                "start": ts,
                "end": filtered[i + 1][0],
            }
            for i, (ts, value) in enumerate(pending)
        ]
        try:
            await self._hass.services.async_call(
                RECORDER_DOMAIN,
                SERVICE_IMPORT_ENTITY_HISTORY,
                {"entity_id": entity_id, "history": history},
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Colmi ring %s history backfill failed", kind)
            return False
        self._watermarks[kind] = pending[-1][0]
        LOGGER.debug(
            "Colmi ring backfilled %d %s history rows", len(history), kind
        )
        return True

    def _async_entity_id(self, kind: str) -> str | None:
        """Resolve the sensor entity id registered for one backfill kind."""
        suffix = _KIND_UNIQUE_SUFFIX[kind]
        unique_id = (
            self._entry_id if suffix is None else f"{self._entry_id}_{suffix}"
        )
        return er.async_get(self._hass).async_get_entity_id(
            "sensor", DOMAIN, unique_id
        )
