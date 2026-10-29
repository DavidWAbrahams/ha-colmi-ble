"""BlueprintEntity class."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STREAM_BUSY_MESSAGE
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
        """Refuse a ring command while the motion stream owns the connection.

        The ring answers exactly one central, so a button press mid-stream
        would either fail confusingly or tear the stream down; better to say so
        plainly and let the user turn the switch off.
        """
        motion = self._motion_manager()
        if motion is not None and motion.is_streaming:
            raise HomeAssistantError(STREAM_BUSY_MESSAGE)
