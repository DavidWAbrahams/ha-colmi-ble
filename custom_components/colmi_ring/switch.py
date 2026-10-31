"""Switch platform for colmi_ring: the two things that hold the ring's link.

``Motion Stream`` holds the ring's single Bluetooth link open and streams raw
accelerometer data, which is what makes gesture detection possible and is also
why it is opt-in: normal polling stops for the duration and the ring's battery
drains noticeably faster. The integration turns it off by itself after the
configured timeout (see ``MotionManager``).

``Clap Detection`` holds that same link without streaming, so the ring's own
on-device clap detector has a central to report to. It costs less than the
stream but freezes the sensors for as long as it is on, so it gets its own,
longer auto-off. The two are mutually exclusive: one link, one session, so
turning one on while the other runs raises rather than silently killing the
user's session.

Both report the state of their session rather than the last request, so if the
ring refuses to connect, or an auto-off timer fires, or a reload shuts a
session down, the switch settles back to off instead of lying.
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

STREAM_UNIQUE_ID_SUFFIX = "colmi_ring_motion_stream"
CLAP_UNIQUE_ID_SUFFIX = "colmi_ring_clap_detection"

# Kept as the stream's suffix so existing entities keep their unique ids.
UNIQUE_ID_SUFFIX = STREAM_UNIQUE_ID_SUFFIX


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the link-holding switches.

    The clap switch is offered unconditionally: it needs no motion option, and
    a user who only wants claps should still be able to arm the detector. The
    stream switch stays behind ``CONF_MOTION_ENABLED``, which also gates the
    gesture event entities.
    """
    motion = entry.runtime_data.motion
    if motion is None:  # pragma: no cover - setup order guard
        LOGGER.debug("Colmi ring switches have no motion manager")
        return
    entities: list[ColmiRingEntity] = [
        ColmiRingClapDetectionSwitch(coordinator=entry.runtime_data.coordinator)
    ]
    if entry.options.get(CONF_MOTION_ENABLED, False):
        entities.append(
            ColmiRingMotionStreamSwitch(coordinator=entry.runtime_data.coordinator)
        )
    async_add_entities(entities)


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
            f"{coordinator.config_entry.entry_id}_{STREAM_UNIQUE_ID_SUFFIX}"
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


class ColmiRingClapDetectionSwitch(ColmiRingEntity, SwitchEntity):
    """Arm the ring's own clap detector, holding the link so it can report."""

    _attr_has_entity_name = True
    _attr_name = "Clap Detection"
    _attr_icon = "mdi:hand-clap"
    # Same reasoning as the stream switch: an operational toggle automations
    # may switch, not a settings knob, and no translation entry is needed.

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{CLAP_UNIQUE_ID_SUFFIX}"
        )
        self._unsubscribe: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Follow the listener so the switch cannot drift from reality."""
        await super().async_added_to_hass()
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - setup order guard
            return
        self._unsubscribe = motion.add_clap_state_listener(self._on_clap_state)
        self.async_on_remove(self._unsubscribe)

    @callback
    def _on_clap_state(self, listening: bool) -> None:
        """The listener turned on or off (also how auto-off reaches the UI)."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """True while the listener actually holds the link."""
        motion = self._motion_manager()
        return bool(motion is not None and motion.is_listening)

    @property
    def available(self) -> bool:
        """Available whenever the manager exists and HA is connected to it."""
        return super().available and self._motion_manager() is not None

    async def async_turn_on(self, **kwargs: object) -> None:
        """Start listening for clap reports.

        Raises :class:`HomeAssistantError` if the motion stream is running: the
        ring serves one central, so the listener cannot have a link the stream
        is holding. A failure to connect is *not* raised from here - the client
        reports an unopenable session by returning, the manager then clears the
        flag, and the switch lands on off with one warning in the log.
        """
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            return
        await motion.async_start_claps()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disarm the detector, give the link back and let polling resume."""
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            return
        motion.async_stop_claps(reason="user")
        self.async_write_ha_state()
