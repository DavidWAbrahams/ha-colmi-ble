"""Owns the live RAW-motion stream and publishes the gestures found in it.

The manager is the only place that knows a stream is open, which matters
because the ring serves a single central: while it is streaming, the data
coordinator performs no BLE I/O at all (see its ``_async_update_data`` gate), so
this class is also what tells the coordinator to pause and resume.

Lifecycle
---------
``async_start`` flips :attr:`is_streaming` *before* the connection is opened and
clears the published heart rate, so nothing can race a poll onto the link, and
so a frozen heart rate can never be mistaken for a live one. ``async_stop`` (or
the auto-off timer) ends the session; when the stream task finally returns, the
coordinator is refreshed so the frozen sensors are replaced with real data.

Everything here runs on the Home Assistant event loop. Accelerometer packets
arrive from a notify callback, so the packet path must stay callback-safe: no
awaiting, and listeners are plain callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.event import async_call_later

from .api import ColmiRingApiClient, MotionSession
from .colmi_codec.motion import (
    AccelSample,
    parse_accel_packet,
    parse_gesture_packet,
)
from .const import (
    CONF_GESTURE_SENSITIVITY,
    CONF_MOTION_LOG_SAMPLES,
    CONF_MOTION_STREAM_TIMEOUT,
    DEFAULT_GESTURE_SENSITIVITY,
    DEFAULT_MOTION_STREAM_TIMEOUT,
    ENABLE_RING_CAMERA_GESTURE,
    GESTURE_EVENT_TYPE,
    GESTURE_SENSITIVITY_SCALE,
    LOGGER,
    MOTION_SENSOR_UPDATE_S,
    MOTION_STARTUP_MUTE_S,
)
from .gestures import GestureEvent, GestureDetector

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


class MotionManager:
    """Start/stop the ring's RAW stream and turn it into gesture events."""

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
        self._session: MotionSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._timer: Callable[[], None] | None = None
        self._rate_timer: Callable[[], None] | None = None
        self._want_stream = False
        self._muted_until = 0.0
        self._log_samples = False
        self._logged_rate = False
        # (command, subtype) -> first payload seen for that kind this session.
        self._unknown_kinds: dict[tuple[int, int], str] = {}

        self.is_streaming = False
        # Newest decoded sample, for the accelerometer sensors. None whenever no
        # stream is running, so the sensors go unknown instead of freezing.
        self.last_sample: AccelSample | None = None
        self._last_sample_published = 0.0

        self._event_listeners: list[Callable[[GestureEvent], None]] = []
        self._sample_listeners: list[Callable[[], None]] = []
        self._state_listeners: list[Callable[[bool], None]] = []

    # -- configuration ------------------------------------------------------

    def _sensitivity(self) -> float:
        raw = self._entry.options.get(
            CONF_GESTURE_SENSITIVITY, DEFAULT_GESTURE_SENSITIVITY
        )
        return GESTURE_SENSITIVITY_SCALE.get(str(raw), 1.0)

    def _timeout_minutes(self) -> float:
        raw = self._entry.options.get(
            CONF_MOTION_STREAM_TIMEOUT, DEFAULT_MOTION_STREAM_TIMEOUT
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(DEFAULT_MOTION_STREAM_TIMEOUT)
        # Non-positive means "stay on until I say otherwise".
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

        Deliberately throttled to :data:`MOTION_SENSOR_UPDATE_S` rather than the
        stream rate: these listeners update sensors, and every sensor update is a
        recorder row. Automations that need the full-rate signal should use the
        gesture events instead.
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

    # -- start / stop -------------------------------------------------------

    async def async_start(self) -> None:
        """Open the stream. Safe to call when it is already running."""
        if self._want_stream:
            return

        self._want_stream = True
        self._log_samples = bool(
            self._entry.options.get(CONF_MOTION_LOG_SAMPLES, False)
        )
        # Rebuilt per session: the gravity reference and the rate estimate both
        # belong to one continuous connection, not to the whole uptime.
        self._detector = GestureDetector(sensitivity=self._sensitivity())
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
        self._cancel_timers()
        session = self._session
        if session is None:
            # Nothing open yet (still connecting): finish now so the switch
            # reflects reality, and the pending session will close itself when
            # it opens (see _on_session).
            self._set_streaming(False)
            self._request_refresh()
            return
        session.request_stop(reason)

    async def async_shutdown(self) -> None:
        """Stop the stream on unload/reload and wait briefly for it to close."""
        self._want_stream = False
        self._cancel_timers()
        if self._session is not None:
            self._session.request_stop("shutdown")
        task = self._task
        if task is None or task.done():
            self._task = None
            return
        _done, pending = await asyncio.wait({task}, timeout=10.0)
        if pending:
            LOGGER.debug("Colmi ring motion stream did not close in time; cancelling")
            task.cancel()
        self._task = None

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
        self._unknown_kinds.clear()
        self._muted_until = time.monotonic() + MOTION_STARTUP_MUTE_S
        self._schedule_auto_off()
        self._rate_timer = async_call_later(
            self._hass, _RATE_PROBE_S, self._async_check_rate
        )
        # The RAW-stream command is the only thing written to the ring during a
        # session unless the camera gesture is explicitly re-enabled. That write
        # used to go out here about 30 ms after the stream opened, and the link
        # died within milliseconds of it on every attempt; while the stream is
        # being stabilised, nothing else may touch the ring mid-session.
        if not ENABLE_RING_CAMERA_GESTURE:
            return
        self._hass.async_create_task(
            self._async_enable_ring_gesture(session),
            "colmi_ring_camera_gesture",
        )

    async def _async_enable_ring_gesture(self, session: MotionSession) -> None:
        """Ask the ring to report its own clap gesture while we are connected.

        Only reached when ``ENABLE_RING_CAMERA_GESTURE`` is on.

        Best-effort: firmware that does not implement the camera-button gesture
        simply answers nothing, and a refusal is not worth an error.
        """
        try:
            enabled = await session.enable_camera_gesture(True)
        except Exception:  # noqa: BLE001
            LOGGER.debug("Colmi ring camera gesture could not be enabled", exc_info=True)
            return
        if not enabled:
            LOGGER.debug(
                "Colmi ring did not accept the camera-gesture packet; only"
                " stream-derived gestures will be available"
            )

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
                " detect a clap from it, so raise/lower will still be reported and"
                " claps only if the ring's own gesture detector works",
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
        """Feed one notification from the stream into the detector."""
        gesture_packet = parse_gesture_packet(packet)
        if gesture_packet is not None:
            # The ring echoes our own enable packet back, and the firmware may
            # also flush anything it queued, so the first moments of a session
            # are not trustworthy.
            if time.monotonic() < self._muted_until:
                LOGGER.debug(
                    "Colmi ring reported a gesture during the startup mute;"
                    " ignoring (packet %s)",
                    packet.hex(),
                )
                return
            self._publish(self._detector.on_device_gesture())
            return

        sample = parse_accel_packet(packet)
        if sample is None:
            self._log_unknown_packet(packet)
            return
        if self._log_samples and LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "colmi motion %.3f %.3f %.3f g", sample.x, sample.y, sample.z
            )
        self._publish_sample(sample)
        for event in self._detector.feed(sample):
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
        """Hand the newest sample to the sensors, at most once per second.

        The detector still sees every sample; only the published state is
        rate-limited, because a state change per sample would write a recorder
        row per sample. The first sample of a session is always published, so the
        sensors leave ``unknown`` promptly instead of after a second of stream.
        """
        self.last_sample = sample
        if not self._sample_listeners:
            return
        now = time.monotonic()
        if (
            self._last_sample_published
            and now - self._last_sample_published < MOTION_SENSOR_UPDATE_S
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

    def _publish(self, event: GestureEvent) -> None:
        """Announce a gesture on the bus, to the entities, and restart auto-off."""
        LOGGER.debug(
            "Colmi ring gesture: %s (%s, peak %.2f g)",
            event.gesture,
            event.source,
            event.peak_g,
        )
        self._schedule_auto_off()
        # Fired here rather than from an entity so it works even if the event
        # entities are disabled, and so one automation trigger sees all three.
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

    def _schedule_auto_off(self) -> None:
        """(Re)arm the auto-off timer; restarted by every gesture."""
        self._cancel_auto_off()
        minutes = self._timeout_minutes()
        if minutes <= 0:
            return
        self._timer = async_call_later(
            self._hass, minutes * 60.0, self._async_auto_off
        )

    def _async_auto_off(self, _now: Any) -> None:
        self._timer = None
        if not self._want_stream:
            return
        LOGGER.info(
            "Colmi ring motion stream turned off after %s minutes of use",
            self._timeout_minutes(),
        )
        self.async_stop(reason="timeout")

    def _cancel_auto_off(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    def _cancel_rate_probe(self) -> None:
        if self._rate_timer is not None:
            self._rate_timer()
            self._rate_timer = None

    def _cancel_timers(self) -> None:
        self._cancel_auto_off()
        self._cancel_rate_probe()

    # -- state --------------------------------------------------------------

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
            self._notify_sample_listeners()
        for listener in list(self._state_listeners):
            try:
                listener(streaming)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring stream state listener raised")

    def async_notify_stream_finished(self) -> None:
        """Called once the stream task has returned: restore normal behaviour."""
        self._session = None
        self._cancel_timers()
        if self._want_stream:
            # The client gave up on its own (it could not open a stream at all).
            self._want_stream = False
        self._set_streaming(False)
        self._request_refresh()

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
