"""
Custom integration to integrate colmi_ring with Home Assistant.

For more details about this integration, please refer to
https://github.com/ludeeus/integration_blueprint
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    async_register_callback,
)
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import callback
from homeassistant.loader import async_get_loaded_integration

from .api import ColmiRingApiClient
from .backfill import BackfillManager
from .const import (
    CONF_BACKFILL_ON_RECONNECT,
    CONF_DEVICE_FAMILY,
    CONF_GESTURE_HOLD_FLIP_S,
    CONF_GESTURE_HOLD_STILL_S,
    CONF_WAVE_TIMEOUT,
    CONF_YCBT_MONITOR_INTERVAL,
    DOMAIN,
    FAMILY_COLMI,
    FAMILY_YCBT,
    LOGGER,
    clamp_ycbt_monitor_interval,
)
from .coordinator import ColmiRingDataUpdateCoordinator
from .data import ColmiRingData
from .motion_manager import MotionManager
from .ycbt_api import YcbtRingApiClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import ColmiRingConfigEntry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    # The platforms decide for themselves what to create: the stream switch is
    # behind the motion option, while the wave switch and the gesture events are
    # offered either way, since a wave has its own source.
    Platform.SWITCH,
    Platform.EVENT,
]


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ColmiRingConfigEntry,
) -> bool:
    """
    Migrate an old config entry to the current version.

    Version 2 renamed the clap gesture to wave, including the persisted
    auto-off option key; move the stored value so the user's setting
    survives the rename. (The gesture/switch entity unique ids also
    changed, which orphans the old entities; HA recreates the new ones,
    and the old leftovers can be removed from the UI.)

    Version 3 introduces the device family (``colmi`` vs ``ycbt``). Every
    entry that predates it was set up against a Colmi ring, so the family
    is simply stamped ``colmi``.

    Version 4 merges the separate ``hr_backfill`` and ``steps_backfill``
    options into a single ``backfill_on_reconnect`` flag: enabled if either
    of the old options was (heart-rate backfill defaulted to on, so the
    merged flag is on unless the user had explicitly turned it off).

    Version 5 splits ``gesture_hold_s`` into a per-gesture pair
    (``gesture_hold_still_s`` and ``gesture_hold_flip_s``). An explicit
    legacy value carries over to both so the user's tuning survives; when
    none was stored, the new per-gesture defaults apply instead.
    """
    if entry.version > 5:
        # Downgrade or already current: nothing to do.
        return True

    new_options = dict(entry.options)
    new_data = dict(entry.data)
    migrated: list[str] = []

    if entry.version <= 1:
        if "clap_timeout" in new_options:
            new_options[CONF_WAVE_TIMEOUT] = new_options.pop("clap_timeout")
        migrated.append("clap gesture renamed to wave")

    if entry.version <= 2:
        new_data.setdefault(CONF_DEVICE_FAMILY, FAMILY_COLMI)
        migrated.append("device family stamped colmi")

    if entry.version <= 3:
        if CONF_BACKFILL_ON_RECONNECT not in new_options:
            new_options[CONF_BACKFILL_ON_RECONNECT] = bool(
                new_options.get("hr_backfill", True)
                or new_options.get("steps_backfill", False)
            )
        new_options.pop("hr_backfill", None)
        new_options.pop("steps_backfill", None)
        migrated.append("backfill options merged into backfill_on_reconnect")

    if entry.version <= 4:
        legacy_hold = new_options.pop("gesture_hold_s", None)
        if legacy_hold is not None:
            new_options.setdefault(CONF_GESTURE_HOLD_STILL_S, legacy_hold)
            new_options.setdefault(CONF_GESTURE_HOLD_FLIP_S, legacy_hold)
        migrated.append("gesture_hold_s split into per-gesture hold options")

    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        version=5,
    )
    LOGGER.info(
        "Config entry %s migrated to version 5 (%s)",
        entry.entry_id,
        "; ".join(migrated),
    )
    return True


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
    family = entry.data.get(CONF_DEVICE_FAMILY, FAMILY_COLMI)
    ring_address = entry.data[CONF_ADDRESS]
    if family == FAMILY_YCBT:
        client = YcbtRingApiClient(
            hass=hass,
            ring_address=ring_address,
            # The ring's monitoring interval is not readable back, so the
            # options value is the source of truth the init burst re-applies
            # on every connect (see the number platform's ycbt entity).
            monitor_interval_minutes=clamp_ycbt_monitor_interval(
                entry.options.get(CONF_YCBT_MONITOR_INTERVAL)
            ),
            on_seen=coordinator.async_note_ring_seen,
        )
    else:
        client = ColmiRingApiClient(
            hass=hass,
            ring_address=ring_address,
            on_seen=coordinator.async_note_ring_seen,
        )

    # Passive presence: HA's bluetooth stack scans continuously (this
    # integration's manifest registers matchers, which keeps active scanning
    # on), so every advertisement from the ring lands here even while no GATT
    # link is open. Together with the clients' on-seen notifications (inbound
    # traffic during a link) this drives the last-seen sensor. The callback
    # fires for every device HA sees (match_dict=None), so it filters by
    # address itself; ACTIVE mirrors the scanning the manifest matchers
    # already force on.
    ring_address_upper = ring_address.upper()

    @callback
    def _async_ring_advertised(service_info: object, _change: object) -> None:
        if getattr(service_info, "address", "").upper() == ring_address_upper:
            coordinator.async_note_ring_seen()

    entry.async_on_unload(
        async_register_callback(
            hass,
            _async_ring_advertised,
            None,
            BluetoothScanningMode.ACTIVE,
        ),
    )
    # The motion manager is Colmi-protocol-only (the RAW accelerometer stream
    # is a Colmi command): YCBT rings have no equivalent, so the family gate
    # leaves it None and every "is the stream busy" check no-ops. For Colmi
    # it is always constructed (it does no I/O until asked) because the
    # coordinator needs to be able to ask whether the stream is holding the
    # connection. The switch and event entities appear for every Colmi setup;
    # the stream only runs while its switch is on.
    motion = (
        MotionManager(hass, entry, client, coordinator)
        if family == FAMILY_COLMI
        else None
    )
    coordinator.motion = motion
    entry.runtime_data = ColmiRingData(
        client=client,
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
        motion=motion,
    )

    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    # TODO: When code is stable, this could become coordinator.async_refresh()
    # which ignores errors from the first refresh.
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
    # The Colmi client keeps its shared link open after its last user leaves
    # (the sessions above are gone by now); close it rather than let the idle
    # timer hold a GATT connection for a dead entry.
    client = entry.runtime_data.client
    if isinstance(client, ColmiRingApiClient):
        await client.async_close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ColmiRingConfigEntry,
) -> None:
    """
    Reload config entry.

    Must NOT manually unload-then-setup: that runs async_setup_entry while
    the entry state is NOT_LOADED, so the coordinator's
    async_config_entry_first_refresh() refuses with ConfigEntryError
    ("Config entry is not loaded") and every entity is orphaned with
    "no longer being provided by the integration". Delegating to HA's own
    reload path keeps its state machine consistent, and it debounces when
    the options flow also triggers an automatic reload.
    """
    schedule_reload = getattr(hass.config_entries, "async_schedule_reload", None)
    if schedule_reload is not None:
        # Current cores: debounced, non-blocking.
        schedule_reload(entry.entry_id)
        return
    # Older cores have neither this nor ConfigEntry.async_schedule_reload, so
    # go through the reload coroutine directly.
    await hass.config_entries.async_reload(entry.entry_id)
