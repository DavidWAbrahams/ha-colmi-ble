"""
How this integration learns which ring model a config entry is.

Model-gated features - the R08 touch surface, the R08 hole axis - need a model
name, and the only names a user can see or set turn out not to hold it
reliably: the entry title mirrors the advertisement name only for entries
created by Bluetooth discovery and can be renamed, while the device name is
the user's to choose ("R08 (black)" carries the model, "Dave's ring" does not,
and a MAC-titled entry never carried it at all). So the advertisement name
seen while the ring is in range gets remembered in the entry's *data*, which
no UI exposes for editing, and every model question is asked here instead.

One module for setup, the options flow and the motion manager, so all three
resolve the model the same way and cannot drift apart.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    async_get as async_get_device_registry,
)
from homeassistant.helpers.device_registry import format_mac

from .const import CONF_ADVERTISED_NAME, DOMAIN, LOGGER, supports_touch

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import ColmiRingConfigEntry

# Any ring this integration knows advertises a name starting with "R" plus a
# digit (R08_5703, R08 (black), R20, R99...). A MAC or "Dave's ring" is not a
# model name. Loose on purpose: which *specific* model it is belongs to the
# callers (supports_touch, resolve_hole_axis); this only separates "a name
# that could identify the model" from one that cannot.
_MODEL_NAME_RE = re.compile(r"^R\d", re.IGNORECASE)


def _advertised_name(hass: HomeAssistant, entry: ColmiRingConfigEntry) -> str:
    """The name in the ring's current BLE advertisement, or ''."""
    try:
        address_key = format_mac(entry.data[CONF_ADDRESS]).upper()
    except ValueError:
        address_key = str(entry.data[CONF_ADDRESS]).upper()
    ble_device = async_ble_device_from_address(hass, address_key, connectable=True)
    if ble_device is None:
        return ""
    return ble_device.name or ble_device.title or ""


@callback
def remember_advertisement_name(
    hass: HomeAssistant,
    entry: ColmiRingConfigEntry,
) -> None:
    """
    Stamp the ring's advertisement name into entry data, once.

    Entry data has no editing UI, so the stamp outlives any rename. Setup
    calls this while the ring is in range - discovery, or the config flow's
    test connection, just heard it advertise - and the first call that finds
    a name wins. Already-stamped entries are left alone, which is also what
    keeps this from ever triggering a reload cascade of its own.
    """
    if entry.data.get(CONF_ADVERTISED_NAME):
        return
    name = _advertised_name(hass, entry)
    if not name:
        return
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ADVERTISED_NAME: name}
    )
    LOGGER.info(
        "Colmi ring %s: remembered advertisement name %r for model detection",
        entry.entry_id,
        name,
    )


@callback
def ring_model_name(hass: HomeAssistant, entry: ColmiRingConfigEntry) -> str:
    """
    Best available model name for this entry, or ''.

    Sources, in trust order: the remembered advertisement name (entry data,
    set once by remember_advertisement_name and untouchable from the UI), the
    entry title, the device registry names, and the live advertisement. The
    first candidate that looks like a model name wins, so a remembered name
    survives later renames while an unstamped entry still gets the best the
    caches can offer.
    """
    device = async_get_device_registry(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    for candidate in (
        str(entry.data.get(CONF_ADVERTISED_NAME) or ""),
        entry.title or "",
        (device.name_by_user or "") if device is not None else "",
        (device.name or "") if device is not None else "",
        _advertised_name(hass, entry),
    ):
        if _MODEL_NAME_RE.match(candidate):
            return candidate
    return ""


@callback
def ring_has_touch(hass: HomeAssistant, entry: ColmiRingConfigEntry) -> bool:
    """
    True if this entry's ring is known to have a readable touch surface.

    The single answer both halves of the integration ask: setup, which builds
    (or omits) the TouchManager, and the options flow, which must not offer
    touch settings for a ring that has no touch entities to read them.
    """
    name = ring_model_name(hass, entry)
    if supports_touch(name):
        return True
    LOGGER.debug(
        "Colmi ring %s: resolved model name %r is not a touch model;"
        " creating no touch entities",
        entry.entry_id,
        name,
    )
    return False
