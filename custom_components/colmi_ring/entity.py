"""BlueprintEntity class."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CLAP_BUSY_MESSAGE, STREAM_BUSY_MESSAGE
from .coordinator import ColmiRingDataUpdateCoordinator

if TYPE_CHECKING:
    from .motion_manager import MotionManager


class ColmiRingEntity(CoordinatorEntity[ColmiRingDataUpdateCoordinator]):
    """BlueprintEntity class."""

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    coordinator.config_entry.domain,
                    coordinator.config_entry.entry_id,
                ),
            },
        )

    def _motion_manager(self) -> MotionManager | None:
        """The stream owner, or None if motion detection is not set up."""
        return self.coordinator.motion

    def _assert_ring_free(self) -> None:
        """Refuse a ring command while a session owns the connection.

        The ring answers exactly one central, so a button press while the
        stream or the clap listener holds the link would either fail confusingly
        or tear that session down; better to say so plainly and name the switch
        to turn off.
        """
        motion = self._motion_manager()
        if motion is None or not motion.holds_link:
            return
        raise HomeAssistantError(
            CLAP_BUSY_MESSAGE if motion.is_listening else STREAM_BUSY_MESSAGE
        )
