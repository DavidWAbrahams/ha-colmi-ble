"""
Owns the R08 touch-listener session and publishes what it reports.

The mirror of ``MotionManager``'s wave half: one long-lived task, one shared
link lease, plain-callback packets from a notify subscription, a switch that
follows the session rather than a wish, and an auto-off timer. What differs is
worth saying up front.

The session is a *shared* lease like the wave listener - touch reports are
Bluetooth HID notifications, so listening needs nothing from the link it has to
hold, and polling (heart rate, battery, steps) continues normally while touch
control is on. Its cost is not the link but the capacitive surface the ring
powers for as long as the firmware is told to listen, which is why the
auto-off option exists and why teardown always tries the disable write.

That shared-ness is also why the manager refuses the RAW motion stream, but not
the other way round. The stream takes the link exclusively, so a touch session
underneath it would have its arm/disarm writes queue behind a lock the stream
never releases until it closes - and a touch session that cannot deliver its
``3B 02 01`` disable leaves the surface powered after the session itself is
gone, a battery cost this integration introduced and promised to take back.
Wave detection, by contrast, is the same kind of shared lease over a different
firmware subsystem (UART camera gestures, not HID), so wave and touch are
allowed to be on at once: they neither share a command nor a report path.

Everything here runs on the Home Assistant event loop. Packets arrive from a
notify callback, so the packet path must stay callback-safe: no awaiting, and
listeners are plain callbacks.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .colmi_codec import r08
from .const import (
    CONF_TOUCH_APP_TYPE,
    CONF_TOUCH_MULTI_TAP_WINDOW,
    CONF_TOUCH_SLEEP_MINUTES,
    CONF_TOUCH_TIMEOUT,
    DEFAULT_TOUCH_APP_TYPE,
    DEFAULT_TOUCH_MULTI_TAP_WINDOW,
    DEFAULT_TOUCH_SLEEP_MINUTES,
    DEFAULT_TOUCH_TIMEOUT,
    LOGGER,
    MULTI_TAP_WINDOW_MAX,
    MULTI_TAP_WINDOW_MIN,
    STREAM_BUSY_MESSAGE,
    TOUCH_EVENT_TYPE,
    TOUCH_SLEEP_MINUTES_MAX,
    TOUCH_SLEEP_MINUTES_MIN,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .api import ColmiRingApiClient, TouchSession
    from .coordinator import ColmiRingDataUpdateCoordinator
    from .data import ColmiRingConfigEntry


class TouchManager:
    """Own the ring's touch listener; nothing else knows the session is open."""

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

        # Wire options, read once per manager like the hole axis: changing any
        # option reloads the entry and rebuilds us, so nothing can switch
        # profile mid-session.
        self._app_type = self._read_app_type()
        self._sleep_minutes = self._read_sleep_minutes()

        self._session: TouchSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._timer: Callable[[], None] | None = None
        self._want_touch = False
        # Distinct usage sets that decoded to no gesture, so an unrecognised
        # bit is named once per session rather than once per tap.
        self._unknown_usage_sets: set[frozenset[str]] = set()
        # The unverified-swipe notice belongs to the instance, not the session:
        # a retry-looped reopen should not spam the warning.
        self._warned_unverified = False

        # Tap-chain coalescing. The ring reports every press as its own HID
        # report and cannot tell a double tap from two single taps, so the
        # chain lives here: the first tap parks the report and starts a window,
        # each further tap restarts the window, and only an expired window
        # announces the chain - as one tap event carrying ``tap_count``.
        self._pending_tap: r08.TouchEvent | None = None
        self._pending_tap_count = 0
        self._tap_timer: Callable[[], None] | None = None

        # True while the touch listener holds its shared lease.
        self.is_listening = False

        self._event_listeners: list[Callable[[r08.TouchEvent], None]] = []
        self._state_listeners: list[Callable[[bool], None]] = []

    # -- configuration ------------------------------------------------------

    def _read_app_type(self) -> int:
        """
        Which HID usage profile the firmware lights up.

        Read from the options like everything else, but not yet offered in the
        options flow: the documented media profile (1) is the only one observed
        to produce reports, so there is nothing meaningful for a user to choose.
        """
        raw = self._entry.options.get(CONF_TOUCH_APP_TYPE, DEFAULT_TOUCH_APP_TYPE)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = int(DEFAULT_TOUCH_APP_TYPE)
        # The byte goes on the wire masked; keep the stored value honest.
        return value & 0xFF

    def _read_sleep_minutes(self) -> int:
        """
        How long the surface stays awake between touches, in minutes.

        Clamped to the range the firmware byte is documented to accept: 0 has
        never been observed to mean "never", so nonsense falls back to the OEM
        default rather than to a surface that sleeps instantly.
        """
        raw = self._entry.options.get(
            CONF_TOUCH_SLEEP_MINUTES, DEFAULT_TOUCH_SLEEP_MINUTES
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = int(DEFAULT_TOUCH_SLEEP_MINUTES)
        return max(TOUCH_SLEEP_MINUTES_MIN, min(TOUCH_SLEEP_MINUTES_MAX, value))

    def _timeout_minutes(self) -> float:
        """Minutes before touch control turns itself off; 0 means never."""
        raw = self._entry.options.get(CONF_TOUCH_TIMEOUT, DEFAULT_TOUCH_TIMEOUT)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(DEFAULT_TOUCH_TIMEOUT)
        return value if value > 0 else 0.0

    def _multi_tap_window(self) -> float:
        """
        Seconds a tap chain stays open after each tap; 0 means never chain.

        Read per tap rather than cached, like the auto-off timeout: it is a
        cheap options lookup, and 0 deliberately disables coalescing so every
        press fires as its own single-tap event, as this manager did before
        multi-taps existed. Clamped because a window long enough to feel like
        lag would just make single taps look broken.
        """
        raw = self._entry.options.get(
            CONF_TOUCH_MULTI_TAP_WINDOW, DEFAULT_TOUCH_MULTI_TAP_WINDOW
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(DEFAULT_TOUCH_MULTI_TAP_WINDOW)
        return max(MULTI_TAP_WINDOW_MIN, min(MULTI_TAP_WINDOW_MAX, value))

    # -- listeners ----------------------------------------------------------

    def add_event_listener(
        self, listener: Callable[[r08.TouchEvent], None]
    ) -> Callable[[], None]:
        """Receive every decoded touch report; returns an unsubscribe callable."""
        self._event_listeners.append(listener)
        return lambda: self._remove_event_listener(listener)

    def _remove_event_listener(
        self, listener: Callable[[r08.TouchEvent], None]
    ) -> None:
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    def add_state_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Learn when touch control actually turns on or off (for the switch)."""
        self._state_listeners.append(listener)
        return lambda: self._remove_state_listener(listener)

    def _remove_state_listener(self, listener: Callable[[bool], None]) -> None:
        if listener in self._state_listeners:
            self._state_listeners.remove(listener)

    # -- start / stop -------------------------------------------------------

    async def async_start(self) -> None:
        """
        Open the touch listener. Safe to call when it is already running.

        Raises :class:`HomeAssistantError` while the RAW motion stream holds the
        link: the stream's lease is exclusive, so this session's arm (and, worse,
        its disarm) would wait on a transaction lock the stream will not release
        until it closes - and a touch session that ends without delivering its
        disable frame leaves the capacitive surface powered. Wave detection is
        deliberately not refused here: it shares the link the same way this
        session does, over a different firmware subsystem.
        """
        if self._want_touch:
            return
        motion = self._coordinator.motion
        if motion is not None and motion.holds_link:
            raise HomeAssistantError(STREAM_BUSY_MESSAGE)

        self._want_touch = True
        self._set_listening(True)

        self._task = self._hass.async_create_task(
            self._run_touches(), "colmi_ring_touch_control"
        )

    async def _run_touches(self) -> None:
        """Listen for touch reports until the session ends or we end it."""
        aborted = False
        try:
            reason = await self._client.async_listen_touches(
                self._on_packet,
                lambda: self._want_touch,
                self._on_touch_session,
                app_type=self._app_type,
                sleep_minutes=self._sleep_minutes,
            )
        except Exception:  # noqa: BLE001
            # Report a fault on *this* logger. Left alone, the traceback is
            # written by Home Assistant's task handler under a
            # ``homeassistant.*`` logger, so a log filtered to
            # custom_components.colmi_ring would show the listener closing
            # cleanly with no reason and no explanation of why.
            LOGGER.exception("Colmi ring touch control task failed")
            reason, aborted = None, True
        finally:
            self.async_notify_touch_finished()
        if aborted:
            # The traceback above is the explanation; do not also claim the
            # listener never opened, which describes the retry-budget case.
            return
        if reason is None:
            LOGGER.warning(
                "Colmi ring touch control could not be opened; the switch has"
                " been turned off (see the log above for the reason)"
            )
        elif reason == "no-echo":
            LOGGER.warning(
                "Colmi ring touch control turned itself off: the ring never"
                " acknowledged the write that enables its touch surface (3B 02),"
                " so this firmware probably has no touch reports to send"
            )
        elif reason == "firmware-off":
            # The WARNING at the notice itself names the frame; this line ties
            # it to the switch the user is watching, and says why the listener
            # did not just reopen: a ring that disabled its own touch will keep
            # it disabled, so retrying would only re-arm what it refused.
            LOGGER.warning(
                "Colmi ring touch control turned itself off: the ring"
                " disabled its own touch surface while the session was open"
                " (see the log above); turn the switch back on to re-arm it"
            )
        elif reason not in ("user", "timeout"):
            # Worth an INFO line: a session that ends by itself (link dropped)
            # is the case a user needs help with, and without it the switch just
            # goes off with nothing in the default log level.
            LOGGER.info("Colmi ring touch control ended (%s)", reason)
        else:
            LOGGER.debug("Colmi ring touch control finished (%s)", reason)

    def async_stop(self, reason: str = "user") -> None:
        """Close the touch listener. Safe to call when it is not listening."""
        if not self._want_touch:
            return
        self._want_touch = False
        self._cancel_auto_off()
        # The taps behind any open chain did happen; the task's teardown path
        # (async_notify_touch_finished) normally flushes them a moment later,
        # but a stop that arrives while still connecting never reaches that
        # path's session, so flush here too. Flushing twice is a no-op.
        self._flush_pending_tap()
        session = self._session
        if session is None:
            # Still connecting: finish now so the switch reflects reality, and
            # _on_touch_session will close the session that opens afterwards.
            self._set_listening(False)
            self._request_refresh()
            return
        session.request_stop(reason)

    async def async_shutdown(self) -> None:
        """Stop the listener on unload/reload and wait briefly for it."""
        self._want_touch = False
        self._cancel_auto_off()
        # Belt and braces: normally the task's own teardown path flushes the
        # open chain, but a task cancelled hard below may never reach it, and
        # a timer left running into an unload would fire against a half-torn-
        # down entry. Flushing an already-flushed chain is a no-op.
        self._flush_pending_tap()
        if self._session is not None:
            self._session.request_stop("shutdown")
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        _done, pending = await asyncio.wait({task}, timeout=10.0)
        if pending:
            LOGGER.debug(
                "Colmi ring touch session did not close in time; cancelling it"
            )
            for stuck in pending:
                stuck.cancel()

    # -- session callbacks --------------------------------------------------

    def _on_touch_session(self, session: TouchSession) -> None:
        """The link is held with the touch surface armed: arm the session."""
        if not self._want_touch:
            # Turned off while we were connecting.
            session.request_stop("user")
            return
        self._session = session
        self._unknown_usage_sets.clear()
        LOGGER.info(
            "Colmi ring touch control open; polling continues on the shared link"
        )
        self._warn_unverified_swipes()
        self._schedule_auto_off()

    def _warn_unverified_swipes(self) -> None:
        """
        Say once that swipe directions would be a guess, if they were one.

        The mapping shipped measured after a labelled on-device test, so
        ``UNVERIFIED_GESTURES`` is empty and this stays silent. The notice
        stays as the fail-safe: if a firmware or orientation is ever reported
        that makes the mapping doubtful again, re-flagging it in the codec
        warns users here instead of silently serving reversed entities. The
        tap entities need no such notice, which is why the set is checked
        rather than the flag assumed non-empty.
        """
        if self._warned_unverified or not r08.UNVERIFIED_GESTURES:
            return
        self._warned_unverified = True
        LOGGER.warning(
            "Colmi ring touch: swipe directions are unverified on this ring"
            " ('%s' may be reversed); tap is solid. See"
            " colmi_codec/r08.py SWIPE_DIRECTION_UNVERIFIED",
            "', '".join(sorted(r08.UNVERIFIED_GESTURES)),
        )

    # -- packet path --------------------------------------------------------

    def _on_packet(self, packet: bytearray) -> None:
        """
        Route one touch report; everything else is dropped silently.

        All-zero is the ring's release frame, not a gesture, and anything the
        report decoder rejects is either that or a frame the session's own tally
        in ``api.py`` already counts and describes at close - a better record of
        what this firmware sends than a warning per frame here.
        """
        event = r08.parse_touch_report(packet)
        if event is None:
            return
        if not event.gestures:
            self._log_unnamed_usages(event)
            return
        # A report whose only gesture is a tap joins (or opens) the tap chain
        # instead of announcing at once; anything else - a swipe, or a combo
        # report no chain interpretation could honestly claim - is not part of
        # a tap chain, so whatever chain is open belongs to an earlier action
        # and is announced first to keep the events in order.
        if self._multi_tap_window() > 0 and event.gestures == frozenset(
            {r08.GESTURE_TAP}
        ):
            self._queue_tap(event)
            return
        self._flush_pending_tap()
        self._announce(event)

    def _queue_tap(self, event: r08.TouchEvent) -> None:
        """Add one tap report to the open chain and restart its window."""
        if self._pending_tap is None:
            self._pending_tap = event
            self._pending_tap_count = 1
        else:
            self._pending_tap_count += 1
        # The window is *between* taps, so it restarts with every press: a
        # chain is done only once the ring has been quiet for the full window,
        # no matter how many taps came before.
        self._cancel_tap_timer()
        self._tap_timer = async_call_later(
            self._hass, self._multi_tap_window(), self._on_tap_window_expired
        )

    @callback
    def _on_tap_window_expired(self, _now: Any) -> None:
        """
        No tap arrived within the window: the chain is done, announce it.

        The ``@callback`` marker is load-bearing, not decoration:
        ``async_call_later`` wraps the listener in a ``HassJob`` whose type is
        inferred from the ``_hass_callback`` attribute this decorator sets.
        Undecorated, Home Assistant infers an Executor job and runs this on a
        worker thread, where the ``hass.bus.async_fire`` below (and the event
        entities' ``async_write_ha_state`` behind it) corrupts state - recent
        releases raise the thread-safety guard instead.
        """
        self._tap_timer = None
        self._flush_pending_tap()

    def _flush_pending_tap(self) -> None:
        """
        Announce the open tap chain, if any, as one tap event with ``tap_count``.

        The report carried by the chain is the last one held (identical to the
        others for tap purposes), re-stamped with the chain's length; a chain
        of one is announced untouched, so a lone tap keeps the exact event data
        it always had except for the always-present ``tap_count: 1``.
        """
        self._cancel_tap_timer()
        event, count = self._pending_tap, self._pending_tap_count
        self._pending_tap, self._pending_tap_count = None, 0
        if event is None:
            return
        if count > 1:
            event = replace(event, tap_count=count)
        self._announce(event)

    def _cancel_tap_timer(self) -> None:
        if self._tap_timer is not None:
            self._tap_timer()
            self._tap_timer = None

    def _log_unnamed_usages(self, event: r08.TouchEvent) -> None:
        """Name a bit we have no gesture for once per session, not per tap."""
        key = frozenset(event.usages)
        if key in self._unknown_usage_sets:
            return
        self._unknown_usage_sets.add(key)
        LOGGER.debug(
            "Colmi ring touch report set no known gesture: usages %s (%s)",
            ", ".join(sorted(event.usages)),
            f"0x{event.raw:06X}",
        )

    def _announce(self, event: r08.TouchEvent) -> None:
        """Put one touch report on the bus and hand it to the event entities."""
        LOGGER.debug(
            "Colmi ring touch: %s x%s (usages %s, %s)",
            ", ".join(sorted(event.gestures)),
            event.tap_count,
            ", ".join(sorted(event.usages)),
            f"0x{event.raw:06X}",
        )
        # Fired here rather than from an entity so it works even if the event
        # entities are disabled, and so one automation trigger sees every touch.
        self._hass.bus.async_fire(
            TOUCH_EVENT_TYPE,
            {**event.as_event_data(), "entry_id": self._entry.entry_id},
        )
        for listener in list(self._event_listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring touch listener raised")

    # -- auto-off -----------------------------------------------------------

    def _schedule_auto_off(self) -> None:
        """
        Arm the auto-off timer, once, when the session opens.

        Never rearmed by a touch - the wave listener's deliberate choice, kept
        here for the same reason in a different currency: a listener restarted by
        every tap would hold the ring's touch surface powered indefinitely, and
        unlike the wave case nothing even frees the link in between. The option
        exists to bound the battery cost, so it should mean "N minutes from now"
        even for someone who taps every few seconds.
        """
        self._cancel_auto_off()
        minutes = self._timeout_minutes()
        if minutes <= 0:
            return
        self._timer = async_call_later(self._hass, minutes * 60.0, self._auto_off)

    @callback
    def _auto_off(self, _now: Any) -> None:
        # Undecorated this would be an Executor job like the tap timer above:
        # async_stop reaches _request_refresh, which calls async_create_task,
        # and a session-end refresh from a worker thread is the same
        # thread-safety violation one tap-event away.
        self._timer = None
        if not self._want_touch:
            return
        LOGGER.info(
            "Colmi ring touch control turned off after %s minutes; the ring's"
            " touch surface is no longer being powered for reports we are not"
            " using",
            self._timeout_minutes(),
        )
        self.async_stop(reason="timeout")

    def _cancel_auto_off(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    # -- state --------------------------------------------------------------

    def async_notify_touch_finished(self) -> None:
        """Called once the task has returned: restore normal behaviour."""
        self._session = None
        self._cancel_auto_off()
        # The session is gone but the taps behind an open chain still happened;
        # announcing them now is more honest than dropping the last chain of a
        # disconnect, and the window's worth of delay is already spent anyway.
        self._flush_pending_tap()
        if self._want_touch:
            # The client gave up on its own, so the switch must not stay on.
            self._want_touch = False
        self._set_listening(False)
        self._request_refresh()

    def _set_listening(self, listening: bool) -> None:
        if self.is_listening == listening:
            return
        self.is_listening = listening
        for listener in list(self._state_listeners):
            try:
                listener(listening)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Colmi ring touch state listener raised")

    def _request_refresh(self) -> None:
        """
        Kick the coordinator now that the session has ended.

        The call sites are synchronous, so the refresh coroutine has to be
        handed to the event loop instead of merely created (which would warn
        "coroutine ... was never awaited"). Touch is a shared lease, so no
        poll was blocked while it ran, but a session that died over a long
        walk is still the moment the user wants fresh data.
        """
        self._hass.async_create_task(
            self._coordinator.async_request_refresh(),
            "colmi_ring_post_touch_refresh",
        )
