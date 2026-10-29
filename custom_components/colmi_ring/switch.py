"""Switch platform for colmi_ring: the live motion stream.

Turning this on holds the ring's single Bluetooth link open and streams raw
accelerometer data, which is what makes gesture detection possible and is also
why it is opt-in: normal polling stops for the duration and the ring's battery
drains noticeably faster. The integration turns it off by itself after the
configured timeout (see ``MotionManager``).

The reported state follows the stream rather than the last request, so if the
ring refuses to connect, or the auto-off timer fires, or a reload shuts the
stream down, the switch settles back to off instead of lying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_MOTION_ENABLED, LOGGER
from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry

UNIQUE_ID_SUFFIX = "colmi_ring_motion_stream"


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the motion stream switch (only when motion is enabled)."""
    if not entry.options.get(CONF_MOTION_ENABLED, False):
        return
    if entry.runtime_data.motion is None:  # pragma: no cover - setup order guard
        LOGGER.debug("Colmi ring motion switch has no motion manager")
        return
    async_add_entities(
        [ColmiRingMotionStreamSwitch(coordinator=entry.runtime_data.coordinator)]
    )


class ColmiRingMotionStreamSwitch(ColmiRingEntity, SwitchEntity):
    """Turn the ring's RAW accelerometer stream on and off."""

    _attr_has_entity_name = True
    _attr_name = "Motion Stream"
    _attr_icon = "mdi:motion-sensor"
    # No translation_key: the name is set here directly, like the other entities
    # in this integration, so no entity translations entry is required.
    # Deliberately not entity_category=CONFIG: this is an operational toggle
    # that automations may well switch, not a settings knob.

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{UNIQUE_ID_SUFFIX}"
        )
        self._unsubscribe: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Follow the stream so the switch cannot drift from reality."""
        await super().async_added_to_hass()
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - setup order guard
            return
        self._unsubscribe = motion.add_state_listener(self._on_stream_state)
        self.async_on_remove(self._unsubscribe)

    @callback
    def _on_stream_state(self, streaming: bool) -> None:
        """The stream turned on or off (also how auto-off reaches the UI)."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """True while the stream is actually running."""
        motion = self._motion_manager()
        return bool(motion is not None and motion.is_streaming)

    @property
    def available(self) -> bool:
        """Available whenever the manager exists and HA is connected to it."""
        return super().available and self._motion_manager() is not None

    async def async_turn_on(self, **kwargs: object) -> None:
        """Start streaming.

        A failure to connect is not raised from here: the client reports an
        unopenable stream by returning, the manager then clears the flag, and
        the switch lands on off with one warning in the log.
        """
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            raise HomeAssistantError(
                "Motion streaming is not enabled for this ring"
            )
        await motion.async_start()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Stop streaming and let normal polling resume."""
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            return
        motion.async_stop(reason="user")
        self.async_write_ha_state()
