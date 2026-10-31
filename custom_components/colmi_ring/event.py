"""Event platform for colmi_ring: the detected gestures.

Each gesture is one event entity, and every gesture also fires the same Home
Assistant event type, so an automation can trigger on either::

    trigger:
      - platform: event
        event_type: colmi_ring_gesture
        event_data:
          gesture: clap

Only the clap is offered today. Raise and lower are recognised by
``GestureDetector`` but are not published, because a ring streaming at 1 Hz
cannot resolve a steady lift or drop (see ``_SUPPRESSED_GESTURES`` in
``motion_manager.py``), so publishing them would only produce unreliable
automations.

The clap can reach us two ways - derived from the RAW stream, or reported by
the ring's own detector while the Clap Detection switch holds the link - and the
event data says which via its ``source`` field. The entities are therefore
created whether or not motion streaming is enabled: a user who only ever turns
on Clap Detection still needs something to trigger on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback

from .const import (
    LOGGER,
)
from .entity import ColmiRingEntity
from .gestures import GESTURE_CLAP, GestureEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry

# The gesture key is both the entity's unique-id suffix and the ``gesture`` value
# in the event data, so automations can rely on either.
ENTITY_DESCRIPTIONS = (
    EventEntityDescription(
        key=GESTURE_CLAP,
        name="Clap",
        icon="mdi:hand-back-right",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the gesture event entities.

    Not gated on ``CONF_MOTION_ENABLED`` the way the stream switch is: the clap
    has a second, independent source, so the entities exist for any setup. The
    listeners they register are harmless while nothing publishes to them.
    """
    coordinator: ColmiRingDataUpdateCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        ColmiRingGestureEvent(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in ENTITY_DESCRIPTIONS
    )


class ColmiRingGestureEvent(ColmiRingEntity, EventEntity):
    """Fires when this gesture is reported by either source.

    The entity's own event is named after the gesture, so it can be used as a
    per-gesture automation trigger. The integration *also* fires the
    ``colmi_ring_gesture`` event on the bus (see ``MotionManager._announce``)
    with the same data, which catches every gesture with one trigger and does
    not depend on how the event component names its generated events.
    """

    _attr_has_entity_name = True
    # Not DIAGNOSTIC: these exist to be triggered on.

    def __init__(
        self,
        coordinator: ColmiRingDataUpdateCoordinator,
        entity_description: EventEntityDescription,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._gesture = entity_description.key
        self._attr_event_types = [entity_description.key]
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_gesture_{entity_description.key}"
        )
        self._unsubscribe: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Start listening for gestures once the entity is registered."""
        await super().async_added_to_hass()
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - setup order guard
            LOGGER.debug(
                "Colmi ring gesture entity %s has no motion manager", self._gesture
            )
            return
        self._unsubscribe = motion.add_event_listener(self._on_gesture)
        self.async_on_remove(self._unsubscribe)

    @callback
    def _on_gesture(self, event: GestureEvent) -> None:
        """Fire if this was our gesture."""
        if event.gesture != self._gesture:
            return
        self._trigger_event(self._gesture, event.as_event_data())
        # Event entities are otherwise stateless; this refreshes the
        # "last triggered" timestamp shown in the UI.
        self.async_write_ha_state()
