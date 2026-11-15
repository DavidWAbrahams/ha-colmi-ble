"""Custom types for colmi_ring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .api import ColmiRingApiClient
    from .coordinator import ColmiRingDataUpdateCoordinator
    from .motion_manager import MotionManager
    from .touch_manager import TouchManager
    from .ycbt_api import YcbtRingApiClient


type ColmiRingConfigEntry = ConfigEntry[ColmiRingData]


@dataclass
class ColmiRingData:
    """Data for the Blueprint integration."""

    # Two protocol families share one integration; the client factory in
    # __init__ picks one from the entry's device family.
    client: ColmiRingApiClient | YcbtRingApiClient
    coordinator: ColmiRingDataUpdateCoordinator
    integration: Integration
    # Owns the RAW motion stream; None when the motion options are off, which is
    # why every user of it treats "no manager" as "stream cannot be busy".
    motion: MotionManager | None = None
    # Owns the R08 touch listener; None on a ring with no touch surface, which
    # is how the switch and the touch event entities know not to exist.
    touch: TouchManager | None = None
