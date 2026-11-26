"""
Switch platform for colmi_ring: the things that hold the ring's link.

``Motion Stream`` takes the ring's shared Bluetooth link for itself and streams
raw accelerometer data, which is what makes gesture detection possible and is
also why it is opt-in: normal polling stops for the duration and the ring's
battery drains noticeably faster. The integration turns it off by itself after
the configured timeout (see ``MotionManager``).

``Wave Detection`` keeps that same link open without streaming, so the ring's
own on-device wave detector has a central to report to. It costs less than the
stream and *shares* the link: polling and the ring's buttons keep working
while it is on, queueing briefly behind its arm/disarm writes. It still gets
its own, longer auto-off. The two sessions are mutually exclusive with each
other, though: RAW mode takes the accelerometer and silences the ring's
gesture reports, so turning one on while the other runs raises rather than
silently killing the user's session.

``Touch Control`` is the third, and only exists on a ring with a touch surface
(R08). It shares the link like wave detection does - reports are HID
notifications - so unlike the other two it costs nothing in polling: what it
costs is the capacitive sensor the firmware keeps powered while it listens,
which is why it too has an auto-off.

All three report the state of their session rather than the last request, so if
the ring refuses to connect, or an auto-off timer fires, or a reload shuts a
session down, the switch settles back to off instead of lying.

``Heart Rate Monitoring`` is different: it holds nothing and costs nothing. It
is the UI for the ``enabled`` byte of the ring's ``0x16`` heart-rate logging
settings - the flag that decides whether the ring samples heart rate at all.
A ring can sit with it off (its own firmware default, an OEM app, or an old
integration write) and then answer every daily-log query with "no data" no
matter what the sampling interval says, and until now nothing in this
integration could read it back *and* change it. It is offered to every Colmi
family ring - the ``0x16`` command predates the R08 - and its state comes from
the settings answer on the last poll.

``HRV Monitoring`` is its ``0x38`` twin for rings that have HRV: the flag that
decides whether the ring records an HRV value every 30 minutes at all, which
is what decides whether any ``0x39`` history (and so any backfill) exists. It
is deliberately *not* offered an interval knob: unlike the ``0x16`` settings,
every verified ``0x38`` frame carries only the enable byte - the HRV sampling
grid is fixed at the firmware's 30-minute slot. Its state comes from the
preference answer on the last poll, so a change made in a vendor app shows up
here too.

``Temperature Monitoring`` is the same idea on ``0x3A`` for rings whose
capability bit reports skin temperature (R09/R10/R12): it decides whether the
ring samples a skin-temperature value into each 30-minute slot at all, which is
what decides whether any ``0x25`` big-data history exists for the Skin
Temperature sensor to read. Like the HRV switch it has no interval knob - every
verified ``0x3A`` frame carries only the enable byte - and its state is the
ring's answer on the last poll. Rings without the capability are never queried,
so the switch stays greyed out rather than pretending to control a feature the
firmware lacks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .api import ColmiRingApiClientError
from .colmi_codec.hr_settings import HeartRateLogSettings
from .const import LOGGER, measurement_busy_message
from .entity import ColmiRingEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry

STREAM_UNIQUE_ID_SUFFIX = "colmi_ring_motion_stream"
WAVE_UNIQUE_ID_SUFFIX = "colmi_ring_wave_detection"
TOUCH_UNIQUE_ID_SUFFIX = "colmi_ring_touch_control"
HR_LOGGING_UNIQUE_ID_SUFFIX = "colmi_ring_hr_logging"
HRV_MONITORING_UNIQUE_ID_SUFFIX = "colmi_ring_hrv_monitoring"
TEMP_MONITORING_UNIQUE_ID_SUFFIX = "colmi_ring_temp_monitoring"

# Kept as the stream's suffix so existing entities keep their unique ids.
UNIQUE_ID_SUFFIX = STREAM_UNIQUE_ID_SUFFIX


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 Unused function argument: `hass`
    entry: ColmiRingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up the link-holding switches.

    Both motion switches are offered to every Colmi setup: neither needs an
    opt-in option, and each only costs battery while it is actually on. Yucheng
    rings have no motion manager, so they get neither switch.
    """
    motion = entry.runtime_data.motion
    if motion is None:  # pragma: no cover - setup order guard
        LOGGER.debug("Colmi ring switches have no motion manager")
        return
    entities: list[ColmiRingEntity] = [
        ColmiRingWaveDetectionSwitch(coordinator=entry.runtime_data.coordinator),
        ColmiRingMotionStreamSwitch(coordinator=entry.runtime_data.coordinator),
        ColmiRingHrMonitoringSwitch(coordinator=entry.runtime_data.coordinator),
        # Registered for every Colmi-family ring like the HR switch, but
        # self-grades to unavailable on rings whose capability bit rules out
        # HRV (they are never queried, so the state stays unknown).
        ColmiRingHrvMonitoringSwitch(coordinator=entry.runtime_data.coordinator),
        # Same pattern for skin temperature: offered to every Colmi ring but
        # unavailable unless the ring answered the 0x3A capability/preference
        # query, which only rings with the temperature capability do.
        ColmiRingTemperatureMonitoringSwitch(
            coordinator=entry.runtime_data.coordinator
        ),
    ]
    # The touch switch follows the manager rather than the model name, so there
    # is one source for "this ring has a touch surface" (see __init__).
    if entry.runtime_data.touch is not None:
        entities.append(
            ColmiRingTouchControlSwitch(coordinator=entry.runtime_data.coordinator)
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
        # Availability also flips when a Read measurement starts/finishes
        # (the stream needs the ring's full attention, LEDs included).
        self.async_on_remove(self.coordinator.add_busy_listener(self._repaint))

    @callback
    def _on_stream_state(self, _streaming: bool) -> None:
        """The stream turned on or off (also how auto-off reaches the UI)."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """True while the stream is actually running."""
        motion = self._motion_manager()
        return bool(motion is not None and motion.is_streaming)

    @property
    def available(self) -> bool:
        """
        Unavailable while a Read measurement owns the ring's sensor.

        Starting the RAW stream mid-measurement would hijack the very sensor
        the measurement is reading, corrupting both; the greyed-out switch is
        the honest signal, and the refusal below covers the racing call.
        """
        return (
            super().available
            and self._motion_manager() is not None
            and self.coordinator.ring_busy is None
        )

    async def async_turn_on(self, **_kwargs: object) -> None:
        """
        Start streaming.

        A failure to connect is not raised from here: the client reports an
        unopenable stream by returning, the manager then clears the flag, and
        the switch lands on off with one warning in the log.
        """
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            raise HomeAssistantError("Motion streaming is not enabled for this ring")
        if self.coordinator.ring_busy is not None:
            raise HomeAssistantError(
                "Cannot start the motion stream: "
                f"{measurement_busy_message(self.coordinator.ring_busy)}"
            )
        await motion.async_start()
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Stop streaming and let normal polling resume."""
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            return
        motion.async_stop(reason="user")
        self.async_write_ha_state()


class ColmiRingWaveDetectionSwitch(ColmiRingEntity, SwitchEntity):
    """Arm the ring's own wave detector, holding the link so it can report."""

    _attr_has_entity_name = True
    _attr_name = "Wave Detection"
    # Not mdi:hand-waving: that name postdates the Material Design Icons set
    # this HA version ships, so it renders as no icon at all. The previous
    # mdi:motion-sensor reads as a walking person, so use the same hand as the
    # "Raised hand" event, which is known to render.
    _attr_icon = "mdi:hand-back-right"
    # Same reasoning as the stream switch: an operational toggle automations
    # may switch, not a settings knob, and no translation entry is needed.

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{WAVE_UNIQUE_ID_SUFFIX}"
        )
        self._unsubscribe: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Follow the listener so the switch cannot drift from reality."""
        await super().async_added_to_hass()
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - setup order guard
            return
        self._unsubscribe = motion.add_wave_state_listener(self._on_wave_state)
        self.async_on_remove(self._unsubscribe)

    @callback
    def _on_wave_state(self, _listening: bool) -> None:
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

    async def async_turn_on(self, **_kwargs: object) -> None:
        """
        Start listening for wave reports.

        Raises :class:`HomeAssistantError` if the motion stream is running: the
        ring serves one central, so the listener cannot have a link the stream
        is holding. A failure to connect is *not* raised from here - the client
        reports an unopenable session by returning, the manager then clears the
        flag, and the switch lands on off with one warning in the log.
        """
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            return
        await motion.async_start_waves()
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Disarm the detector, give the link back and let polling resume."""
        motion = self._motion_manager()
        if motion is None:  # pragma: no cover - guarded by available
            return
        motion.async_stop_waves(reason="user")
        self.async_write_ha_state()


class ColmiRingTouchControlSwitch(ColmiRingEntity, SwitchEntity):
    """
    Put the ring's touch surface into reporting mode.

    The counterpart to the official app's touch switch: while it is on, a tap or
    swipe on the ring arrives as a Bluetooth HID report and fires this
    integration's touch events.
    """

    _attr_has_entity_name = True
    _attr_name = "Touch Control"
    # Not mdi:hand-waving (absent from the Material Design Icons set this HA
    # version ships, so it renders as no icon); mdi:gesture-tap reads as
    # "finger on a surface", which is literally what this switch arms.
    _attr_icon = "mdi:gesture-tap"
    # Operational toggle, not a settings knob: same reasoning as the other two.

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{TOUCH_UNIQUE_ID_SUFFIX}"
        )
        self._unsubscribe: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Follow the listener so the switch cannot drift from reality."""
        await super().async_added_to_hass()
        touch = self._touch_manager()
        if touch is None:  # pragma: no cover - setup order guard
            return
        self._unsubscribe = touch.add_state_listener(self._on_touch_state)
        self.async_on_remove(self._unsubscribe)

    @callback
    def _on_touch_state(self, _listening: bool) -> None:
        """The listener turned on or off (also how auto-off reaches the UI)."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """True while the listener actually holds the link."""
        touch = self._touch_manager()
        return bool(touch is not None and touch.is_listening)

    @property
    def available(self) -> bool:
        """Available whenever the manager exists and HA is connected to it."""
        return super().available and self._touch_manager() is not None

    async def async_turn_on(self, **_kwargs: object) -> None:
        """
        Start listening for touch reports.

        Raises :class:`HomeAssistantError` if the motion stream is running: its
        lease is exclusive, and this session's arm/disarm writes would queue
        behind it indefinitely. A failure to connect is *not* raised from here -
        the client reports an unopenable session by returning, the manager then
        clears the flag, and the switch lands on off with one warning in the log.
        """
        touch = self._touch_manager()
        if touch is None:  # pragma: no cover - guarded by available
            return
        await touch.async_start()
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Tell the ring to stop listening, so it can stop powering the surface."""
        touch = self._touch_manager()
        if touch is None:  # pragma: no cover - guarded by available
            return
        touch.async_stop(reason="user")
        self.async_write_ha_state()


class ColmiRingHrMonitoringSwitch(ColmiRingEntity, SwitchEntity):
    """
    Turn the ring's periodic heart-rate recording on and off.

    This is byte 2 of the ``0x16`` settings answer: with it off the ring
    records nothing and answers every daily heart-rate log query with
    "no data", whatever the sampling interval says. Like the interval number
    entity, a write is read-modify-write - the ring's last-reported interval
    and its four extra settings bytes are echoed back unchanged - and the
    state shown is the ring's answer on the last poll, not the last request.
    """

    _attr_has_entity_name = True
    _attr_name = "Heart Rate Monitoring"
    _attr_icon = "mdi:heart-plus"
    # Deliberately not entity_category=CONFIG, unlike it might seem: CONFIG
    # pushes the entity into a collapsed "Configuration" section on the device
    # page and out of auto-populated dashboard cards. This is the one switch
    # whose state explains a silent no-data sensor, so it belongs where the
    # other switches are, under Controls, enabled by default in the registry.
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{HR_LOGGING_UNIQUE_ID_SUFFIX}"
        )

    def _current_settings(self) -> HeartRateLogSettings | None:
        data = self.coordinator.data
        if not data:
            return None
        settings = data.get("hr_settings")
        return settings if isinstance(settings, HeartRateLogSettings) else None

    @property
    def is_on(self) -> bool:
        """Whether the ring reported heart-rate monitoring enabled."""
        settings = self._current_settings()
        return settings.enabled if settings else False

    @property
    def available(self) -> bool:
        """Available when the ring has answered the settings query."""
        return super().available and self._current_settings() is not None

    async def _async_set_logging(self, enabled: bool) -> None:
        """Write ``enabled`` while preserving the ring's other settings."""
        self._assert_ring_free()
        settings = self._current_settings()
        if settings is None:  # pragma: no cover - guarded by available
            return
        client = self.coordinator.config_entry.runtime_data.client
        try:
            result = await client.async_set_hr_log_settings(
                HeartRateLogSettings(
                    enabled=enabled,
                    interval=settings.interval,
                    extra=settings.extra,
                )
            )
        except ColmiRingApiClientError as err:
            LOGGER.warning(
                "Failed to set Colmi ring HR monitoring to %s: %s",
                "on" if enabled else "off",
                err,
            )
            raise HomeAssistantError(
                f"Could not change heart-rate monitoring on the ring: {err}"
            ) from err
        LOGGER.debug(
            "Colmi ring HR monitoring set to %s, ring now reports: %s",
            "on" if enabled else "off",
            result,
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **_kwargs: object) -> None:
        """Tell the ring to start recording heart rate."""
        await self._async_set_logging(True)

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Tell the ring to stop recording heart rate."""
        await self._async_set_logging(False)


class ColmiRingHrvMonitoringSwitch(ColmiRingEntity, SwitchEntity):
    """
    Turn the ring's all-day HRV monitoring on and off.

    This is the ``0x38`` preference: with it off the ring never samples HRV
    and answers every ``0x39`` history query with "no data", so neither the
    HRV history sensor's long-term graph nor the backfill has anything to
    work with. Unlike the ``0x16`` heart-rate settings there is no interval
    to expose: every verified ``0x38`` frame carries only the enable byte,
    and the ring's HRV grid is fixed at one value per 30 minutes. The state
    shown is the ring's answer on the last poll, not the last request.
    """

    _attr_has_entity_name = True
    _attr_name = "HRV Monitoring"
    _attr_icon = "mdi:heart-multiple"
    # Same reasoning as the HR monitoring switch: this is the entity whose
    # state explains an empty HRV history, so it belongs under Controls, not in
    # a collapsed Configuration section.
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{HRV_MONITORING_UNIQUE_ID_SUFFIX}"
        )

    def _current_state(self) -> bool | None:
        """The ring's last reported preference, or None when unknown."""
        data = self.coordinator.data
        if not data:
            return None
        value = data.get("hrv_monitoring")
        return value if isinstance(value, bool) else None

    @property
    def is_on(self) -> bool:
        """Whether the ring reported all-day HRV monitoring enabled."""
        return self._current_state() is True

    @property
    def available(self) -> bool:
        """
        Available when the ring has answered the 0x38 query.

        Rings whose capability bit rules out HRV are never asked, so their
        state stays unknown and the switch stays greyed out rather than
        pretending to control something the firmware does not have.
        """
        return super().available and self._current_state() is not None

    async def _async_set_monitoring(self, enabled: bool) -> None:
        """Write the preference and refresh so the state shows the ring's."""
        self._assert_ring_free()
        client = self.coordinator.config_entry.runtime_data.client
        try:
            result = await client.async_set_hrv_monitoring(enabled)
        except ColmiRingApiClientError as err:
            LOGGER.warning(
                "Failed to set Colmi ring HRV monitoring to %s: %s",
                "on" if enabled else "off",
                err,
            )
            raise HomeAssistantError(
                f"Could not change HRV monitoring on the ring: {err}"
            ) from err
        LOGGER.debug(
            "Colmi ring HRV monitoring set to %s, ring now reports: %s",
            "on" if enabled else "off",
            result,
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **_kwargs: object) -> None:
        """Tell the ring to start recording HRV all day."""
        await self._async_set_monitoring(True)

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Tell the ring to stop recording HRV all day."""
        await self._async_set_monitoring(False)


class ColmiRingTemperatureMonitoringSwitch(ColmiRingEntity, SwitchEntity):
    """
    Turn the ring's all-day skin-temperature monitoring on and off.

    This is the ``0x3A`` preference: with it off the ring never samples skin
    temperature and answers every ``0x25`` big-data history query empty, so the
    Skin Temperature sensor has nothing to graph. Like the ``0x38`` HRV switch
    there is no interval to expose - the temperature grid is fixed at one value
    per 30 minutes - and the state shown is the ring's answer on the last poll,
    not the last request.
    """

    _attr_has_entity_name = True
    _attr_name = "Temperature Monitoring"
    _attr_icon = "mdi:thermometer-lines"
    # Same reasoning as the HR/HRV monitoring switches: this entity explains an
    # empty Skin Temperature sensor, so it belongs under Controls, not in a
    # collapsed Configuration section.
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: ColmiRingDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{TEMP_MONITORING_UNIQUE_ID_SUFFIX}"
        )

    def _current_state(self) -> bool | None:
        """The ring's last reported preference, or None when unknown."""
        data = self.coordinator.data
        if not data:
            return None
        value = data.get("temperature_monitoring")
        return value if isinstance(value, bool) else None

    @property
    def is_on(self) -> bool:
        """Whether the ring reported all-day temperature monitoring enabled."""
        return self._current_state() is True

    @property
    def available(self) -> bool:
        """
        Available when the ring has answered the 0x3A query.

        Rings whose capability bit rules out temperature are never asked, so
        their state stays unknown and the switch stays greyed out rather than
        pretending to control something the firmware does not have.
        """
        return super().available and self._current_state() is not None

    async def _async_set_monitoring(self, enabled: bool) -> None:
        """Write the preference and refresh so the state shows the ring's."""
        self._assert_ring_free()
        client = self.coordinator.config_entry.runtime_data.client
        try:
            result = await client.async_set_temperature_monitoring(enabled)
        except ColmiRingApiClientError as err:
            LOGGER.warning(
                "Failed to set Colmi ring temperature monitoring to %s: %s",
                "on" if enabled else "off",
                err,
            )
            raise HomeAssistantError(
                f"Could not change temperature monitoring on the ring: {err}"
            ) from err
        LOGGER.debug(
            "Colmi ring temperature monitoring set to %s, ring now reports: %s",
            "on" if enabled else "off",
            result,
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **_kwargs: object) -> None:
        """Tell the ring to start recording skin temperature all day."""
        await self._async_set_monitoring(True)

    async def async_turn_off(self, **_kwargs: object) -> None:
        """Tell the ring to stop recording skin temperature all day."""
        await self._async_set_monitoring(False)
