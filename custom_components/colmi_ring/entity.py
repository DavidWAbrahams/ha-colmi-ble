"""BlueprintEntity class."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STREAM_BUSY_MESSAGE, measurement_busy_message
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
        """
        Refuse a ring command while the motion stream owns the connection.

        RAW mode takes the ring's single sensor pipeline for itself, so a button
        press while the stream holds the link exclusively would fail confusingly
        or tear the session down; better to say so plainly and name the switch to
        turn off. Wave detection is deliberately absent: it shares the link, so
        commands just queue briefly behind its arm/disarm writes.
        """
        motion = self._motion_manager()
        if motion is None or not motion.holds_link:
            return
        raise HomeAssistantError(STREAM_BUSY_MESSAGE)

    def _watch_ring_busy(self) -> None:
        """
        Repaint this entity when a measurement or the stream starts/stops.

        Availability that depends on ``coordinator.ring_busy`` (or on the
        stream) would otherwise only refresh on the next coordinator poll,
        which can be minutes away. Unsubscribes with the entity.
        """
        self.async_on_remove(self.coordinator.add_busy_listener(self._repaint))
        motion = self._motion_manager()
        if motion is not None:
            self.async_on_remove(motion.add_state_listener(self._on_stream_change))

    @callback
    def _repaint(self, _running: object = None) -> None:
        """A measurement started or finished; availability may have changed."""
        self.async_write_ha_state()

    @callback
    def _on_stream_change(self, _streaming: bool) -> None:
        """The stream turned on or off; availability may have changed."""
        self.async_write_ha_state()

    def _begin_measurement(self, name: str) -> None:
        """
        Claim the ring's one-measurement-at-a-time slot, or refuse.

        Claimed synchronously in the press handler (no await between check
        and set) so a second press cannot slip past the greyed-out UI in the
        instant before the first repaints. The slot is the coordinator's
        because the Read buttons, and the Motion Stream switch, all gate on
        it; the API client refuses a concurrent measurement too, but only
        the UI can *show* the wait instead of hitting it.
        """
        running = self.coordinator.ring_busy
        if running is not None:
            raise HomeAssistantError(measurement_busy_message(running))
        self.coordinator.async_set_ring_busy(name)

    def _end_measurement(self) -> None:
        """Give the measurement slot back and repaint the gated controls."""
        self.coordinator.async_set_ring_busy(None)
