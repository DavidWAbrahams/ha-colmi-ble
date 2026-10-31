"""Owns the ring's two long-lived sessions and publishes what they report.

The manager is the only place that knows a session is open, which matters
because the ring serves a single central: while either the RAW motion stream or
the clap listener holds the link, the data coordinator performs no BLE I/O at
all (see its ``_async_update_data`` gate), so this class is also what tells the
coordinator to pause and resume.

Lifecycle
---------
``async_start`` and :meth:`async_start_claps` flip :attr:`is_streaming` and
:attr:`is_listening` *before* the connection is opened and clear the published
heart rate, so nothing can race a poll onto the link, and so a frozen heart rate
can never be mistaken for a live one. ``async_stop`` and
:meth:`async_stop_claps` (or the matching auto-off timer) end the session; when
the task finally returns, the coordinator is refreshed so the frozen sensors are
replaced with real data.

Everything here runs on the Home Assistant event loop. Packets arrive from a
notify callback, so the packet path must stay callback-safe: no awaiting, and
listeners are plain callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .api import ClapSession, ColmiRingApiClient, MotionSession
from .colmi_codec.motion import (
    AccelSample,
    parse_accel_packet,
    parse_gesture_packet,
)
from .const import (
    CLAP_BUSY_MESSAGE,
    CLAP_REPORT_COOLDOWN_S,
    CONF_CLAP_TIMEOUT,
    CONF_GESTURE_SENSITIVITY,
    CONF_MOTION_LOG_SAMPLES,
    CONF_MOTION_SENSOR_UPDATE_S,
    CONF_MOTION_STREAM_TIMEOUT,
    DEFAULT_CLAP_TIMEOUT,
    DEFAULT_GESTURE_SENSITIVITY,
    DEFAULT_MOTION_SENSOR_UPDATE_S,
    DEFAULT_MOTION_STREAM_TIMEOUT,
    GESTURE_EVENT_TYPE,
    GESTURE_SENSITIVITY_SCALE,
    LOGGER,
    STREAM_BUSY_MESSAGE,
)
from .gestures import (
    GESTURE_CLAP,
    GESTURE_LOWER,
    GESTURE_RAISE,
    SOURCE_RING,
    GestureEvent,
    GestureDetector,
    TiltTracker,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry

# How long to let samples accumulate before judging the stream rate.
_RATE_PROBE_S = 5.0

# How far the median at-rest magnitude may sit from 1 g before the decode is
# treated as suspect. Generous, because a hand is never perfectly still.
_REST_TRUST_G = 0.35

# How many distinct kinds of undecodable frame to name per session. The question
# they answer is which command/subtype pair carries the accelerometer stream, so
# a handful is a diagnosis; a stream inventing more than that is a different
# problem, and naming them all would only bury the log.
_MAX_UNKNOWN_KINDS = 6

# Frame kinds the stream is known to send alongside the samples and that carry
# nothing we decode: measured over a 77 s session to arrive exactly once per
# second next to the 0xA1/3 sample, always near-identical in content. They are
# per-tick status echoes, not a codec problem, so they must not be warned about.
_BENIGN_STREAM_FRAMES = frozenset({(0xA1, 0x01), (0xA1, 0x02)})

# Gestures the detector is still able to produce but that are never published.
# A steady hand raise or lower produces no acceleration to integrate at the
# ring's measured 1 Hz, so what fires is really a tilt, and an automation built
# on it would act on the wrong gesture. The detector itself is untouched so a
# future firmware with a faster stream can bring them back by dropping them here.
_SUPPRESSED_GESTURES = frozenset({GESTURE_LOWER, GESTURE_RAISE})


class MotionManager:
    """Own the ring's RAW stream and its on-device clap listener."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ColmiRingConfigEntry,
        client: ColmiRingApiClient,
        coordinator: ColmiRingDataUpdateCoordinator,
    ) -> None:
        """Store what we need; nothing is connected or scheduled yet."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._coordinator = coordinator

        self._detector = GestureDetector(sensitivity=self._sensitivity())
        self._tilt = TiltTracker()
        self._session: MotionSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._stream_timer: Callable[[], None] | None = None
        self._rate_timer: Callable[[], None] | None = None
        self._want_stream = False
        self._log_samples = False
        self._logged_rate = False
        # Seconds between accelerometer sensor updates; set per session.
        self._sensor_update_s = DEFAULT_MOTION_SENSOR_UPDATE_S
        # (command, subtype) -> first payload seen for that kind this session.
        self._unknown_kinds: dict[tuple[int, int], str] = {}

        # -- clap detection -------------------------------------------------
        self._clap_session: ClapSession | None = None
        self._clap_task: asyncio.Task[None] | None = None
        self._clap_timer: Callable[[], None] | None = None
        self._want_clap = False
        # When the last clap report was published, for the dedupe below.
        self._last_clap_at = 0.0
        # Clap reports this session that were published, for the closing line.
        self._clap_reports = 0

        self.is_streaming = False
        # True while the clap listener holds the link. Independent of
        # ``is_streaming``: the two are mutually exclusive, never both.
        self.is_listening = False
        # Newest decoded sample, for the accelerometer sensors. None whenever no
        # stream is running, so the sensors go unknown instead of freezing.
        self.last_sample: AccelSample | None = None
        # How far the ring has tilted since it was last zeroed, for the vertical
        # angle sensor. None until the stream has seen a pose still enough to
        # zero on.
        self.last_angle_deg: float | None = None
        self._last_sample_published = 0.0

        self._event_listeners: list[Callable[[GestureEvent], None]] = []
        self._sample_listeners: list[Callable[[], None]] = []
        self._state_listeners: list[Callable[[bool], None]] = []
        self._clap_state_listeners: list[Callable[[bool], None]] = []

    # -- configuration ------------------------------------------------------

    def _sensitivity(self) -> float:
        raw = self._entry.options.get(
            CONF_GESTURE_SENSITIVITY, DEFAULT_GESTURE_SENSITIVITY
        )
        return GESTURE_SENSITIVITY_SCALE.get(str(raw), 1.0)

    def _sensor_update_interval(self) -> float:
        """Seconds between accelerometer sensor updates; 0 means every sample.

        The stream rate is a firmware property, so the useful throttle is too:
        stock firmware reports at 1 Hz and cannot be throttled below that, while
        a faster firmware needs a smaller value to be worth seeing.
        """
        raw = self._entry.options.get(
            CONF_MOTION_SENSOR_UPDATE_S, DEFAULT_MOTION_SENSOR_UPDATE_S
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(DEFAULT_MOTION_SENSOR_UPDATE_S)
        # Zero (or nonsense below it) means "do not throttle at all".
        return value if value > 0 else 0.0

    def _stream_timeout_minutes(self) -> float:
        raw = self._entry.options.get(
            CONF_MOTION_STREAM_TIMEOUT, DEFAULT_MOTION_STREAM_TIMEOUT
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(DEFAULT_MOTION_STREAM_TIMEOUT)
        # Non-positive means "stay on until I say otherwise".
        return value if value > 0 else 0.0

    def _clap_timeout_minutes(self) -> float:
        raw = self._entry.options.get(CONF_CLAP_TIMEOUT, DEFAULT_CLAP_TIMEOUT)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(DEFAULT_CLAP_TIMEOUT)
        return value if value > 0 else 0.0

    # -- listeners ----------------------------------------------------------

    def add_event_listener(
        self, listener: Callable[[GestureEvent], None]
    ) -> Callable[[], None]:
        """Receive every detected gesture; returns an unsubscribe callable."""
        self._event_listeners.append(listener)
        return lambda: self._remove_event_listener(listener)

    def _remove_event_listener(self, listener: Callable[[GestureEvent], None]) -> None:
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    def add_sample_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Learn that a new accelerometer sample is ready to be read.

        The listener takes no arguments and reads :attr:`last_sample` itself, so
        the same callback also works when the sample goes away with the stream.

        Deliberately throttled to the ``motion_sensor_update_s`` option rather
        than the stream rate: these listeners update sensors, and every sensor
        update is a recorder row. Set that option to 0 to be called for every
        sample; automations that need the full-rate signal should otherwise use
        the gesture events.
        """
        self._sample_listeners.append(listener)
        return lambda: self._remove_sample_listener(listener)

    def _remove_sample_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._sample_listeners:
            self._sample_listeners.remove(listener)

    def add_state_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Learn when the stream actually turns on or off (for the switch)."""
        self._state_listeners.append(listener)
        return lambda: self._remove_state_listener(listener)

    def _remove_state_listener(self, listener: Callable[[bool], None]) -> None:
        if listener in self._state_listeners:
            self._state_listeners.remove(listener)

    def add_clap_state_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Learn when clap detection actually turns on or off (for the switch)."""
        self._clap_state_listeners.append(listener)
        return lambda: self._remove_clap_state_listener(listener)

    def _remove_clap_state_listener(self, listener: Callable[[bool], None]) -> None:
        if listener in self._clap_state_listeners:
            self._clap_state_listeners.remove(listener)

    # -- start / stop -------------------------------------------------------

    async def async_start(self) -> None:
        """Open the stream. Safe to call when it is already running.

        Raises :class:`HomeAssistantError` while clap detection is on: the ring
        serves a single central, so the stream cannot have the link the listener
        is holding - and its camera mode does not report while RAW mode owns the
        accelerometer either.
        """
        if self._want_stream:
            return
        if self._want_clap:
            raise HomeAssistantError(CLAP_BUSY_MESSAGE)

        self._want_stream = True
        self._log_samples = bool(
            self._entry.options.get(CONF_MOTION_LOG_SAMPLES, False)
        )
        # Read once per session like the other options: changing any option
        # reloads the entry, which rebuilds this manager anyway.
        self._sensor_update_s = self._sensor_update_interval()
        # Rebuilt per session: the gravity reference and the rate estimate both
        # belong to one continuous connection, not to the whole uptime.
        self._detector = GestureDetector(sensitivity=self._sensitivity())
        self._tilt.reset()
        self.last_angle_deg = None
        self._logged_rate = False

        self._set_streaming(True)
        # Polling is now blocked, so the last known heart rate would otherwise
        # sit there looking fresh for as long as the stream stays open.
        self._coordinator.async_clear_heart_rate()

        self._task = self._hass.async_create_task(
            self._run(), "colmi_ring_motion_stream"
        )

    async def _run(self) -> None:
        """Run the stream until it ends, then restore normal operation."""
        aborted = False
        try:
            reason = await self._client.async_stream_motion(
                self._on_packet,
                lambda: self._want_stream,
                self._on_session,
            )
        except Exception:  # noqa: BLE001
            # Report a fault on *this* logger. Left alone, the traceback is written
            # by Home Assistant's task handler under a ``homeassistant.*`` logger,
            # so a log filtered to ``custom_components.colmi_ring`` shows the stream
            # closing cleanly with no reason and no explanation of why.
            LOGGER.exception("Colmi ring motion stream task failed")
            reason, aborted = None, True
        finally:
            self.async_notify_stream_finished()
        if aborted:
            # The traceback above is the explanation; do not also claim the stream
            # never opened, which describes the retry-budget case, not this one.
            return
        if reason is None:
            LOGGER.warning(
                "Colmi ring motion stream could not be opened; the switch has"
                " been turned off (see the log above for the reason)"
            )
        elif reason != "user":
            # Worth an INFO line: a session that ends by itself (link dropped,
            # stream went silent) is the case a user needs help with, and
            # without it the switch simply goes off with no explanation in the
            # default log level.
            LOGGER.info("Colmi ring motion stream ended (%s)", reason)
        else:
            LOGGER.debug("Colmi ring motion stream finished (%s)", reason)

    def async_stop(self, reason: str = "user") -> None:
        """Close the stream. Safe to call when it is not running."""
        if not self._want_stream:
            return
        self._want_stream = False
        self._cancel_stream_timers()
        session = self._session
        if session is None:
            # Nothing open yet (still connecting): finish now so the switch
            # reflects reality, and the pending session will close itself when
            # it opens (see _on_session).
            self._set_streaming(False)
            self._request_refresh()
            return
        session.request_stop(reason)

    # -- clap detection -----------------------------------------------------

    async def async_start_claps(self) -> None:
        """Hold the link so the ring's own clap detector can reach us.

        Safe to call when it is already listening. Raises
        :class:`HomeAssistantError` while the motion stream is running, for the
        same reasons :meth:`async_start` refuses the other way round.
        """
        if self._want_clap:
            return
        if self._want_stream:
            raise HomeAssistantError(STREAM_BUSY_MESSAGE)

        self._want_clap = True
        # Set before connecting, like the stream does: the coordinator stops
        # polling and the ring's buttons refuse to fire for as long as the link
        # is ours, and a heart rate frozen at its last value must not read
        # as live.
        self._set_listening(True)
        self._coordinator.async_clear_heart_rate()
        self._last_clap_at = 0.0
        self._clap_reports = 0

        self._clap_task = self._hass.async_create_task(
            self._run_claps(), "colmi_ring_clap_detection"
        )

    async def _run_claps(self) -> None:
        """Listen for clap reports until the session ends or we end it."""
        aborted = False
        try:
            reason = await self._client.async_listen_claps(
                self._on_clap_packet,
                lambda: self._want_clap,
                self._on_clap_session,
            )
        except Exception:  # noqa: BLE001
            # Same reasoning as _run: the traceback belongs on *this* logger, or
            # a log filtered to custom_components.colmi_ring shows the listener
            # closing cleanly with no explanation of why.
            LOGGER.exception("Colmi ring clap detection task failed")
            reason, aborted = None, True
        finally:
            self.async_notify_clap_finished()
        if aborted:
            # The traceback above is the explanation; do not also claim the
            # listener never opened, which describes the retry-budget case.
            return
        if reason is None:
            LOGGER.warning(
                "Colmi ring clap detection could not be opened; the switch has"
                " been turned off (see the log above for the reason)"
            )
        elif reason == "unarmed":
            LOGGER.warning(
                "Colmi ring clap detection turned itself off: the ring never"
                " accepted the write that enables its clap detector (02 04), so"
                " this firmware probably has no on-device clap to report"
            )
        elif reason not in ("user", "timeout"):
            # Worth an INFO line: a session that ends by itself (link dropped) is
            # the case a user needs help with, and without it the switch just
            # goes off with nothing in the default log level.
            LOGGER.info("Colmi ring clap detection ended (%s)", reason)
        else:
            LOGGER.debug("Colmi ring clap detection finished (%s)", reason)

    def async_stop_claps(self, reason: str = "user") -> None:
        """Close the clap listener. Safe to call when it is not listening."""
        if not self._want_clap:
            return
        self._want_clap = False
        self._cancel_clap_auto_off()
        session = self._clap_session
        if session is None:
            # Still connecting: finish now so the switch reflects reality, and
            # _on_clap_session will close the session that opens afterwards.
            self._set_listening(False)
            self._request_refresh()
            return
        session.request_stop(reason)

    async def async_shutdown(self) -> None:
        """Stop both sessions on unload/reload and wait briefly for them."""
        self._want_stream = False
        self._want_clap = False
        self._cancel_stream_timers()
        self._cancel_clap_auto_off()
        if self._session is not None:
            self._session.request_stop("shutdown")
        if self._clap_session is not None:
            self._clap_session.request_stop("shutdown")
        tasks = {
            task
            for task in (self._task, self._clap_task)
            if task is not None and not task.done()
        }
        self._task = None
        self._clap_task = None
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=10.0)
        if pending:
            LOGGER.debug(
                "Colmi ring session(s) did not close in time; cancelling %d",
                len(pending),
            )
            for task in pending:
                task.cancel()

    # -- session callbacks --------------------------------------------------

    def _on_session(self, session: MotionSession) -> None:
        """A connection is open with RAW on: arm the session."""
        if not self._want_stream:
            # Turned off while we were connecting.
            session.request_stop("user")
            return
        self._session = session
        LOGGER.info(
            "Colmi ring motion stream open; waiting for accelerometer data"
        )
        self._detector.reset()
        self._tilt.reset()
        self._unknown_kinds.clear()
        self._schedule_stream_auto_off()
        self._rate_timer = async_call_later(
            self._hass, _RATE_PROBE_S, self._async_check_rate
        )

    def _on_clap_session(self, session: ClapSession) -> None:
        """The link is held with the ring's clap detector armed: arm the session."""
        if not self._want_clap:
            # Turned off while we were connecting.
            session.request_stop("user")
            return
        self._clap_session = session
        LOGGER.info("Colmi ring clap detection open; polling is paused")
        self._schedule_clap_auto_off()

    def _async_check_rate(self, _now: Any) -> None:
        """Report once per session what the stream actually looks like.

        This is the only place the measured sample rate reaches the log, and the
        natural place to sanity-check the decode: an accelerometer at rest reads
        about 1 g, so a median magnitude well off that means the packet decode is
        wrong rather than the gesture logic being badly tuned. Both figures are
        worth one INFO line per session, and the magnitude deserves a warning
        because a wrong scale silently disables every gesture.
        """
        self._rate_timer = None
        if self._logged_rate:
            return
        self._logged_rate = True

        rate = self._detector.sample_rate_hz
        rest = self._detector.rest_magnitude_g()
        magnitude = "" if rest is None else f", median magnitude {rest:.2f} g"
        if rate is None:
            LOGGER.info(
                "Colmi ring motion stream has reported no accelerometer sample"
                " in %d seconds%s%s",
                int(_RATE_PROBE_S),
                magnitude,
                self._unknown_summary(),
            )
        elif self._detector.rate_is_too_low():
            self._detector.stream_clap = False
            LOGGER.info(
                "Colmi ring motion stream: %.1f Hz over %d samples%s; too slow to"
                " detect a clap from the accelerometer, so a clap can only come"
                " from the Clap Detection switch",
                rate,
                self._detector.samples_seen,
                magnitude,
            )
        else:
            LOGGER.info(
                "Colmi ring motion stream: %.1f Hz over %d samples%s",
                rate,
                self._detector.samples_seen,
                magnitude,
            )
        if rest is not None and abs(rest - 1.0) > _REST_TRUST_G:
            LOGGER.warning(
                "Colmi ring accelerometer readings average %.2f g instead of the"
                " ~1 g expected of a ring at rest; the scale or bit-packing"
                " assumed in colmi_codec/motion.py is probably wrong, so the"
                " gesture thresholds will not behave as tuned",
                rest,
            )

    def _on_packet(self, packet: bytearray) -> None:
        """Feed one notification from the stream into the detector.

        A clap is not reported from here. The ring only announces its own clap
        while camera mode is armed and nothing else is holding the accelerometer,
        so claps come from the Clap Detection switch instead (see
        ``MOTION_MIN_HZ_FOR_STREAM_CLAP`` for why the samples cannot yield one).
        A gesture frame that does turn up mid-stream is still worth one DEBUG
        line, because how the two modes interact is exactly what a report here
        would tell us - but it is not published, so one clap cannot fire twice.
        """
        if parse_gesture_packet(packet) is not None:
            LOGGER.debug(
                "Colmi ring reported an on-device gesture while the RAW stream"
                " was running; ignored: %s",
                packet.hex(),
            )
            return

        sample = parse_accel_packet(packet)
        if sample is None:
            self._log_unknown_packet(packet)
            return
        if self._log_samples and LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "colmi motion %.3f %.3f %.3f g", sample.x, sample.y, sample.z
            )
        # The tilt tracker sees every sample, like the detector: its low-pass
        # and its stillness window both need the uninterrupted stream, while
        # the published angle is rate-limited with the sample itself.
        self._tilt.feed(sample)
        self.last_angle_deg = self._tilt.angle_deg
        self._publish_sample(sample)
        for event in self._detector.feed(sample):
            # Deliberately not published; see _SUPPRESSED_GESTURES. The detector
            # still tracks them so its internal state stays as tested.
            if event.gesture in _SUPPRESSED_GESTURES:
                continue
            self._publish(event)

    def _log_unknown_packet(self, packet: bytearray) -> None:
        """Name each kind of genuinely surprising undecodable frame once.

        A ring that answers the RAW command but never yields a decodable sample
        is the failure that looks like a silent ring: the session stays open, the
        sensors stay ``unknown``, and no gesture ever fires. The deciding detail
        is the command/subtype pair, because that is what ``colmi_codec/motion.py``
        matches on, so each new pair earns one WARNING with its payload. Known
        benign per-tick frames are dropped silently, and repeats stay quiet, so a
        healthy session produces no log noise from this path at all.
        """
        kind = (packet[0], packet[1] if len(packet) > 1 else 0)
        if kind in _BENIGN_STREAM_FRAMES or kind in self._unknown_kinds:
            return
        if len(self._unknown_kinds) >= _MAX_UNKNOWN_KINDS:
            return
        payload = packet.hex()
        self._unknown_kinds[kind] = payload
        LOGGER.warning(
            "Colmi ring motion stream sent a frame we cannot decode: command"
            " 0x%02X, subtype 0x%02X (%s). If this is meant to be the"
            " accelerometer stream, the layout assumed in colmi_codec/motion.py"
            " is wrong and no gesture can ever be detected",
            kind[0],
            kind[1],
            payload,
        )

    def _unknown_summary(self) -> str:
        """The undecodable kinds seen this session, for the rate-check line."""
        if not self._unknown_kinds:
            return ""
        kinds = ", ".join(f"0x{cmd:02X}/{sub}" for cmd, sub in self._unknown_kinds)
        return f" (received {kinds} instead)"

    def _publish_sample(self, sample: AccelSample) -> None:
        """Hand the newest sample to the sensors, no faster than the option says.

        The detector still sees every sample; only the published state is
        rate-limited, because a state change per sample writes a recorder row per
        sample. The first sample of a session is always published, so the sensors
        leave ``unknown`` promptly instead of after a whole interval of stream.
        """
        self.last_sample = sample
        if not self._sample_listeners:
            return
        now = time.monotonic()
        if (
            self._last_sample_published
            and now - self._last_sample_published < self._sensor_update_s
        ):
            return
        self._last_sample_published = now
        self._notify_sample_listeners()

    def _notify_sample_listeners(self) -> None:
        """Tell the accelerometer sensors to re-read :attr:`last_sample`."""
        for listener in list(self._sample_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring sample listener raised")

    def _on_clap_packet(self, packet: bytearray) -> None:
        """Publish one clap from the ring's own detector, deduplicated.

        Anything on the link that is not a gesture report is dropped silently:
        the session's own tally in ``api.py`` already counts and describes every
        notification when the session closes, which is a better record of what
        this firmware sends than a warning per frame here.
        """
        if parse_gesture_packet(packet) is None:
            return
        now = time.monotonic()
        if now - self._last_clap_at < CLAP_REPORT_COOLDOWN_S:
            LOGGER.debug(
                "Colmi ring reported a clap again within %.1f s; treating it as"
                " the same clap (%s)",
                CLAP_REPORT_COOLDOWN_S,
                packet.hex(),
            )
            return
        self._last_clap_at = now
        self._clap_reports += 1
        self._announce(
            GestureEvent(
                gesture=GESTURE_CLAP,
                source=SOURCE_RING,
                # The ring reports a gesture, not a measurement: there are no
                # acceleration figures behind it to report.
                peak_g=0.0,
                velocity_g_s=0.0,
            )
        )

    def _publish(self, event: GestureEvent) -> None:
        """Announce a stream gesture, and restart the stream's auto-off timer.

        The restart is what makes that option mean "turn off after N minutes of
        *no gestures*" rather than "N minutes of use". A clap deliberately does
        not come through here - see :meth:`_announce`.
        """
        self._announce(event)
        if self._want_stream:
            self._schedule_stream_auto_off()

    def _announce(self, event: GestureEvent) -> None:
        """Put one gesture on the bus and hand it to the event entities."""
        LOGGER.debug(
            "Colmi ring gesture: %s (%s, peak %.2f g)",
            event.gesture,
            event.source,
            event.peak_g,
        )
        # Fired here rather than from an entity so it works even if the event
        # entities are disabled, and so one automation trigger sees every gesture.
        self._hass.bus.async_fire(
            GESTURE_EVENT_TYPE,
            {**event.as_event_data(), "entry_id": self._entry.entry_id},
        )
        for listener in list(self._event_listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring gesture listener raised")

    # -- auto-off -----------------------------------------------------------

    def _schedule_stream_auto_off(self) -> None:
        """(Re)arm the stream's auto-off timer; restarted by every gesture."""
        self._cancel_stream_auto_off()
        minutes = self._stream_timeout_minutes()
        if minutes <= 0:
            return
        self._stream_timer = async_call_later(
            self._hass, minutes * 60.0, self._async_stream_auto_off
        )

    def _async_stream_auto_off(self, _now: Any) -> None:
        self._stream_timer = None
        if not self._want_stream:
            return
        LOGGER.info(
            "Colmi ring motion stream turned off after %s minutes of use",
            self._stream_timeout_minutes(),
        )
        self.async_stop(reason="timeout")

    def _cancel_stream_auto_off(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer()
            self._stream_timer = None

    def _schedule_clap_auto_off(self) -> None:
        """Arm the clap listener's auto-off timer, once, when it opens.

        Never rearmed by a clap. That is the deliberate difference from the
        stream above: a listener restarted by every clap would let someone who
        claps every few minutes hold the ring's link indefinitely, and polling
        would never come back - which is the exact cost this switch warns about.
        """
        self._cancel_clap_auto_off()
        minutes = self._clap_timeout_minutes()
        if minutes <= 0:
            return
        self._clap_timer = async_call_later(
            self._hass, minutes * 60.0, self._async_clap_auto_off
        )

    def _async_clap_auto_off(self, _now: Any) -> None:
        self._clap_timer = None
        if not self._want_clap:
            return
        LOGGER.info(
            "Colmi ring clap detection turned off after %s minutes; the ring's"
            " single link is free again for polling",
            self._clap_timeout_minutes(),
        )
        self.async_stop_claps(reason="timeout")

    def _cancel_clap_auto_off(self) -> None:
        if self._clap_timer is not None:
            self._clap_timer()
            self._clap_timer = None

    def _cancel_rate_probe(self) -> None:
        if self._rate_timer is not None:
            self._rate_timer()
            self._rate_timer = None

    def _cancel_stream_timers(self) -> None:
        self._cancel_stream_auto_off()
        self._cancel_rate_probe()

    # -- state --------------------------------------------------------------

    @property
    def holds_link(self) -> bool:
        """True while either session owns the ring's single GATT link.

        This is what the coordinator and the ring's buttons are gated on: neither
        session can coexist with a poll, so both have to answer for it.
        """
        return self.is_streaming or self.is_listening

    def _set_streaming(self, streaming: bool) -> None:
        if self.is_streaming == streaming:
            return
        self.is_streaming = streaming
        if streaming:
            # Publish the first sample of the session without waiting out the
            # throttle window.
            self._last_sample_published = 0.0
        else:
            # No stream, no data: the sensors go unknown rather than showing the
            # last reading of a session that has ended.
            self.last_sample = None
            self.last_angle_deg = None
            self._notify_sample_listeners()
        for listener in list(self._state_listeners):
            try:
                listener(streaming)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring stream state listener raised")

    def async_zero_angle(self) -> bool:
        """Zero the vertical angle on the pose the hand is in right now.

        The zero button's action. Deliberately needs nothing from the ring, so
        it works mid-stream; the angle simply restarts from wherever the hand
        happens to be. Returns False if the stream has not yet seen a usable
        sample, in which case nothing changed.
        """
        if not self._tilt.zero():
            return False
        self.last_angle_deg = self._tilt.angle_deg
        # Re-notify now rather than wait for the next throttled sample, so the
        # sensor visibly snaps to zero the moment the button is pressed.
        self._notify_sample_listeners()
        return True

    def async_notify_stream_finished(self) -> None:
        """Called once the stream task has returned: restore normal behaviour."""
        self._session = None
        self._cancel_stream_timers()
        if self._want_stream:
            # The client gave up on its own (it could not open a stream at all).
            self._want_stream = False
        self._set_streaming(False)
        self._request_refresh()

    def async_notify_clap_finished(self) -> None:
        """Called once the clap task has returned: restore normal behaviour."""
        self._clap_session = None
        self._cancel_clap_auto_off()
        if self._want_clap:
            # The client gave up on its own, so the switch must not stay on.
            self._want_clap = False
        self._set_listening(False)
        self._request_refresh()

    def _set_listening(self, listening: bool) -> None:
        if self.is_listening == listening:
            return
        self.is_listening = listening
        for listener in list(self._clap_state_listeners):
            try:
                listener(listening)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring clap state listener raised")

    def _request_refresh(self) -> None:
        """Kick the coordinator now that the ring is free again.

        Both call sites are synchronous, so the refresh coroutine has to be
        handed to the event loop instead of merely created (which would warn
        "coroutine ... was never awaited").
        """
        self._hass.async_create_task(
            self._coordinator.async_request_refresh(),
            "colmi_ring_post_stream_refresh",
        )
