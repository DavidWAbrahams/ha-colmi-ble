"""
The integration-wide answer to "does this ring have a touch surface?".

Lives in its own module because both halves of the integration need exactly
one answer: setup, which builds (or omits) the TouchManager, and the options
flow, which must not offer touch settings for a ring that has no touch
entities to read them. Re-deriving the gate in two places is how they drift
apart, and a ring with entities but no options - or options but no entities -
is a bug either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    async_get as async_get_device_registry,
)
from homeassistant.helpers.device_registry import (
    format_mac,
)

from .const import DOMAIN, LOGGER, supports_touch

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import ColmiRingConfigEntry


@callback
def ring_has_touch(hass: HomeAssistant, entry: ColmiRingConfigEntry) -> bool:
    """
    True if this entry's ring is known to have a readable touch surface.

    ``entry.title`` is the honest first choice - it is the advertisement name
    for any entry created by Bluetooth discovery, for those rings too - but an
    entry added by MAC carries no model in its name, and no UI exists to
    rename an entry afterwards. So before saying no, ask the two caches that
    already know this ring: the device registry (which holds any name the user
    gave the device, e.g. "R08 (black)") and the passive Bluetooth scanner
    (which holds the advertisement name whenever the ring is in range). Both
    are cache reads; neither opens a connection, and both simply miss on a
    first setup while the ring is asleep.
    """
    if supports_touch(entry.title):
        return True

    device = async_get_device_registry(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    if device is not None and (
        supports_touch(device.name_by_user) or supports_touch(device.name)
    ):
        return True

    try:
        address_key = format_mac(entry.data[CONF_ADDRESS]).upper()
    except ValueError:
        address_key = str(entry.data[CONF_ADDRESS]).upper()
    ble_device = async_ble_device_from_address(hass, address_key, connectable=True)
    if ble_device is not None and (
        supports_touch(ble_device.name) or supports_touch(ble_device.title)
    ):
        return True

    LOGGER.debug(
        "Colmi ring %s: none of entry title %r, device name %r or advertised"
        " name %r matched the touch model pattern; creating no touch entities",
        entry.entry_id,
        entry.title,
        device.name_by_user if device is not None else None,
        ble_device.name if ble_device is not None else None,
    )
    return False
