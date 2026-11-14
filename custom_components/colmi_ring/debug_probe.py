"""
Dev-only packet probe service: capture the ring's raw traffic through HA's
own Bluetooth stack.

Registered as ``colmi_ring.debug_packet_probe`` so a developer can run the
same capture the workstation scripts (``scripts/_accel_probe.py``,
``scripts/_ycbt_probe.py``) do, but on the adapter HA already owns - the
local radio or an ESPHome Bluetooth proxy - without pausing the config
entry or fighting the single-central adapter for the radio.

What the service does for the duration:

1. pauses the coordinator's polling, so no transaction pollutes the capture,
2. takes the shared link as an *exclusive* lease - the RAW motion stream's
   mode - so nothing can interleave command exchanges,
3. logs the full GATT table,
4. ``start_notify`` on every other notify-capable characteristic, so a
   report on a characteristic nobody knew about is seen too,
5. taps every UART-TX notification *before* the transport's length filter
   (``_RingConnection.raw_observer``),
6. optionally sends raw frames (hex),
7. sleeps while you do the thing you are hunting (tap the R08, wave, ask
   the ring a command nobody has implemented yet...),
8. restores everything and logs a per-packet-type tally.

Everything is logged at WARNING under the ``PROBE`` tag so the whole
capture appears in the HA log without enabling debug logging. Colmi-family
rings only: YCBT rings use a different transport (``ycbt_api``) with no
shared link to lease, and asking a YCBT entry for a Colmi probe is a
mistake worth refusing loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import time
from typing import TYPE_CHECKING

import voluptuous as vol
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ServiceValidationError

from .api import ColmiRingApiClient, ColmiRingApiClientError
from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import timedelta

    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.descriptor import BleakGATTDescriptor
    from homeassistant.core import HomeAssistant, ServiceCall

    from .api import _RingConnection
    from .data import ColmiRingConfigEntry

SERVICE_DEBUG_PACKET_PROBE = "debug_packet_probe"

# Reference-count key in hass.data[DOMAIN]: several config entries each
# register the service at setup, and unloading one entry must not unregister
# it for the others.
_REG_COUNT_KEY = f"{DOMAIN}_debug_probe_registrations"

# One in-flight probe per config entry: a second call would race the first
# one's observer, notify subscriptions and link lease.
_PROBES: dict[str, asyncio.Task[str]] = {}

# Per-packet lines per probe after which the rest are only tallied, so an
# accidental 15-minute probe over a 10 Hz stream cannot flood the log.
_MAX_PACKET_LINES = 400

# How many static descriptors to dump, and how many bytes of each. A Report
# Map fits well inside these; a runaway descriptor must not spam the log.
#
# The cap has a footgun, learned the hard way: a live run read exactly this
# many characteristics and stopped *before* the Bluetooth HID service, which
# happened to enumerate last - so the Report Map it was run to obtain never
# got read. Interesting characteristics are therefore dumped first (see
# ``_hogp_priority``) rather than trusting discovery order.
_MAX_READ_CHARS = 24
_MAX_READ_BYTES = 300

# How much of a raw write to echo into the log before truncating it.
_MAX_WRITE_LOG_BYTES = 32

# Standard base UUID, so a characteristic's 16-bit assigned number can be
# pulled out and compared against the Bluetooth HID Service's.
_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

# The Bluetooth HID Service's characteristics: HID Information (2a4a), Report
# Map (2a4b), HID Control Point (2a4c), Report (2a4d), Protocol Mode (2a4e).
_HOGP_SHORT_UUIDS = frozenset({"2a4a", "2a4b", "2a4c", "2a4d", "2a4e"})

# A HID Report characteristic's Report Reference descriptor, holding the
# report ID and report type that distinguish one 2a4d declaration from the
# next. Report Reference is 2908, but the descriptor list is read as a whole
# because firmware also stashes a User Description (2901) there sometimes.
_HID_DESCRIPTOR_SHORT_UUIDS = frozenset({"2908", "2901", "2902"})

# Per-characteristic descriptor cap, for a firmware that advertises a lot.
_MAX_DESCRIPTORS_PER_CHAR = 6
_MAX_DESCRIPTOR_BYTES = 16

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("address"): str,
        vol.Optional("duration_s", default=60.0): vol.All(
            vol.Coerce(float), vol.Range(min=5.0, max=900.0)
        ),
        # Frames and writes are lists of strings, but HA's field validation can
        # hand us a mapping or a JSON string instead of a list depending on how
        # the call was authored, so the schema is permissive here and
        # ``_as_items`` does the normalising (and reports bad shapes itself).
        # Each frame is plain hex, e.g. "3b 02 01 00 ..." (spaces optional).
        vol.Optional("frames"): vol.Any(None, str, {str: object}, [object]),
        # Raw writes to any characteristic: "UUID = hex bytes", where UUID may
        # be a Bluetooth SIG 16-bit assigned number ("2a4c") or a full 128-bit
        # UUID. Needed to drive paths the UART codec does not know about.
        vol.Optional("writes"): vol.Any(None, str, {str: object}, [object]),
        vol.Optional("subscribe_all", default=True): bool,
    }
)


def register_probe_service(hass: HomeAssistant) -> None:
    """Register the debug service once per HA instance (reference counted)."""
    counts: dict[str, int] = hass.data.setdefault(DOMAIN, {})
    count = counts.get(_REG_COUNT_KEY, 0)
    counts[_REG_COUNT_KEY] = count + 1
    if count == 0:
        hass.services.async_register(
            DOMAIN,
            SERVICE_DEBUG_PACKET_PROBE,
            functools.partial(_async_handle_service, hass),
            schema=SERVICE_SCHEMA,
        )


def remove_probe_service(hass: HomeAssistant) -> None:
    """Undo one :func:`register_probe_service` call."""
    counts: dict[str, int] = hass.data.setdefault(DOMAIN, {})
    count = max(counts.get(_REG_COUNT_KEY, 1) - 1, 0)
    counts[_REG_COUNT_KEY] = count
    if count == 0:
        hass.services.async_remove(DOMAIN, SERVICE_DEBUG_PACKET_PROBE)


async def cancel_probe(entry_id: str) -> None:
    """End a running probe on entry unload/reload, waiting for its cleanup."""
    task = _PROBES.pop(entry_id, None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _async_handle_service(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _resolve_entry(hass, call.data.get("entry_id"), call.data.get("address"))
    entry_id = entry.entry_id
    # Validate everything that can fail on user error *here*, so the UI
    # reports it; a failure raised inside the background task would only
    # surface in the log.
    if not isinstance(entry.runtime_data.client, ColmiRingApiClient):
        raise ServiceValidationError(
            f"Entry {entry.title} is a YCBT ring;"
            " debug_packet_probe supports Colmi-family rings only"
        )
    running = _PROBES.get(entry_id)
    if running is not None and not running.done():
        raise ServiceValidationError(
            f"A debug probe is already running for {entry.title}; check the"
            " log for its PROBE lines (it ends on its own after duration_s)"
        )
    try:
        frames = _parse_frames(call.data.get("frames"))
        writes = _parse_writes(call.data.get("writes"))
    except (TypeError, ValueError) as err:
        raise ServiceValidationError(str(err)) from err
    _PROBES[entry_id] = hass.async_create_task(
        _async_run_probe(
            entry,
            duration_s=float(call.data["duration_s"]),
            frames=frames,
            writes=writes,
            subscribe_all=bool(call.data["subscribe_all"]),
        ),
        name=f"colmi_ring debug probe {entry_id}",
    )
    LOGGER.warning(
        "PROBE debug_packet_probe started for %s (%s); results appear in the"
        " Home Assistant log as 'PROBE' lines",
        entry.title,
        entry_id,
    )


def _resolve_entry(
    hass: HomeAssistant,
    entry_id: str | None,
    address: str | None,
) -> ColmiRingConfigEntry:
    entries = hass.config_entries.async_entries(DOMAIN)
    found: ColmiRingConfigEntry | None = None
    if entry_id:
        found = next((e for e in entries if e.entry_id == entry_id), None)
        if found is None:
            raise ServiceValidationError(f"No colmi_ring entry with id {entry_id}")
    elif address:
        wanted = address.strip().lower()
        found = next(
            (e for e in entries if str(e.data.get(CONF_ADDRESS, "")).lower() == wanted),
            None,
        )
        if found is None:
            raise ServiceValidationError(f"No colmi_ring entry with address {address}")
    elif len(entries) == 1:
        found = entries[0]
    else:
        known = ", ".join(
            f"{e.title} [{e.data.get(CONF_ADDRESS, '?')}] = {e.entry_id}"
            for e in entries
        )
        raise ServiceValidationError(f"Specify entry_id or address; entries: {known}")
    if found.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"Entry {found.entry_id} is not loaded")
    return found


def _as_items(value: object, field: str) -> list[str]:
    """
    Coerce a service field into a list of strings.

    Home Assistant validates service data against a field's selector before the
    handler runs, and the shapes that survive differ by how the call was
    authored: a YAML list arrives as a list, a UI form built from an object
    selector arrives as a mapping, and a pasted JSON array arrives as a string.
    Accept all three so a developer is never told their probe arguments are
    malformed for reasons that have nothing to do with the protocol.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith(("[", "{")):
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(text)
        else:
            return [line for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        items: list[object] = list(value.values())
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise TypeError(f"{field} must be a list of strings, got {value!r}")
    return [str(item) for item in items if str(item).strip()]


def _parse_frames(frames: object) -> list[bytearray]:
    parsed: list[bytearray] = []
    for index, frame in enumerate(_as_items(frames, "frames")):
        cleaned = frame.replace(" ", "").replace(":", "").replace(",", "")
        try:
            raw = bytearray.fromhex(cleaned)
        except ValueError as err:
            raise ValueError(f"Frame {index} is not hex: {frame!r}") from err
        if not 1 <= len(raw) <= 31:
            raise ValueError(
                f"Frame {index} is {len(raw)} bytes (expected 1..31): {frame!r}"
            )
        parsed.append(raw)
    return parsed


def _parse_writes(writes: object) -> list[tuple[str, bytearray]]:
    """
    Parse ``"UUID = hex bytes"`` write requests.

    A 4-digit UUID is a Bluetooth SIG assigned number and is expanded to the
    standard base UUID, because typing the whole 128-bit form for ``2a4c`` is
    the kind of friction that stops a probe from being run.
    """
    parsed: list[tuple[str, bytearray]] = []
    for index, item in enumerate(_as_items(writes, "writes")):
        uuid_part, _, hex_part = item.partition("=")
        uuid = uuid_part.strip().lower()
        if not uuid or not hex_part.strip():
            raise ValueError(f"Write {index} needs 'UUID = hex bytes': {item!r}")
        if len(uuid) == 4:
            uuid = f"0000{uuid}-0000-1000-8000-00805f9b34fb"
        cleaned = hex_part.replace(" ", "").replace(":", "").replace(",", "")
        try:
            raw = bytearray.fromhex(cleaned)
        except ValueError as err:
            raise ValueError(f"Write {index} is not hex: {item!r}") from err
        if not 1 <= len(raw) <= 512:
            raise ValueError(
                f"Write {index} is {len(raw)} bytes (expected 1..512): {item!r}"
            )
        parsed.append((uuid, raw))
    return parsed


def _short_uuid(uuid: str) -> str:
    """A characteristic UUID's assigned number, when it has the base form."""
    lowered = uuid.lower()
    if lowered.endswith(_BASE_UUID_SUFFIX):
        return lowered[4:8]
    return lowered


def _hogp_priority(char: BleakGATTCharacteristic) -> int:
    """Sort key putting the Bluetooth HID Service's characteristics first."""
    return 0 if _short_uuid(char.uuid) in _HOGP_SHORT_UUIDS else 1


def _label(char: BleakGATTCharacteristic) -> str:
    """``uuid#handle``, the only way to name one of several same-UUID declarations."""
    return f"{char.uuid}#{char.handle}"


async def _send_writes(
    conn: _RingConnection,
    address: str,
    writes: list[tuple[str, bytearray]],
) -> None:
    """Send each ``UUID = bytes`` write, logging it and tolerating refusals."""
    for uuid, payload in writes:
        shown = payload[:_MAX_WRITE_LOG_BYTES]
        suffix = f" ... ({len(payload)} bytes)" if len(payload) > len(shown) else ""
        LOGGER.warning(
            "PROBE %s TX[%s] %s%s",
            address,
            uuid[:8],
            shown.hex(" "),
            suffix,
        )
        try:
            handle = await conn.debug_write(uuid, payload)
        except (TimeoutError, BleakError, OSError) as err:
            LOGGER.warning("PROBE %s TX[%s] failed: %s", address, uuid[:8], err)
            continue
        # The handle answers "which declaration did I just write", which for a
        # HID peripheral with four Report characteristics is not pedantry:
        # writing the wrong one is a silent no-op that looks like a dead device.
        LOGGER.warning(
            "PROBE %s TX[%s] written to handle %d", address, uuid[:8], handle
        )
        await asyncio.sleep(0.2)


async def _dump_readable_chars(conn: _RingConnection, address: str) -> None:
    """
    Log every readable characteristic's value, HID ones first.

    Static descriptors answer questions traffic cannot: the Bluetooth HID
    Report Map (``2a4b``) states how a ring's input reports are laid out,
    and the HID Information (``2a4a``) whether it implements HID over GATT
    at all - i.e. whether touch events can reach a BLE host. Reads are
    capped so an unexpectedly huge descriptor cannot spam the log, and each
    is guarded because some firmwares reject individual reads.
    """
    readable = sorted(conn.debug_readable_chars(), key=_hogp_priority)
    for char in readable[:_MAX_READ_CHARS]:
        try:
            value = await conn.debug_read(char)
        except (TimeoutError, BleakError, OSError) as err:
            LOGGER.warning("PROBE %s READ %s failed: %s", address, _label(char), err)
            continue
        shown = value[:_MAX_READ_BYTES]
        suffix = f" ... ({len(value)} bytes total)" if len(value) > len(shown) else ""
        LOGGER.warning(
            "PROBE %s READ %s = %s%s",
            address,
            _label(char),
            shown.hex(" "),
            suffix,
        )


def _label_desc(desc: BleakGATTDescriptor) -> str:
    """``uuid#handle`` for a descriptor, matching how characteristics are named."""
    return f"{desc.uuid}#{desc.handle}"


async def _dump_hid_descriptors(conn: _RingConnection, address: str) -> None:
    """
    Log the descriptors that tell the HID reports apart.

    A HID peripheral declares one characteristic per report, all under the
    same ``2a4d`` UUID - this ring publishes four. Their Report Reference
    descriptors (``2908``) hold the report ID and the report type (1 Input /
    2 Output / 3 Feature), which is what identifies the input reports, and
    therefore which declaration a touch notification would arrive on. Without
    it, a probe that hears nothing cannot tell "not the report I subscribed
    to" from "no notifications at all".
    """
    chars = [
        char
        for char in sorted(conn.debug_gatt_chars(), key=_hogp_priority)
        if _hogp_priority(char) == 0
    ]
    for char in chars:
        descriptors = conn.debug_char_descriptors(char)
        interesting = [
            desc
            for desc in descriptors
            if _short_uuid(desc.uuid) in _HID_DESCRIPTOR_SHORT_UUIDS
        ]
        if not interesting:
            continue
        LOGGER.warning(
            "PROBE %s CHAR %s descriptors: %s",
            address,
            _label(char),
            ", ".join(_label_desc(desc) for desc in descriptors),
        )
        for desc in interesting[:_MAX_DESCRIPTORS_PER_CHAR]:
            try:
                value = await conn.debug_read_descriptor(desc)
            except (TimeoutError, BleakError, OSError) as err:
                LOGGER.warning(
                    "PROBE %s DESC %s failed: %s", address, _label_desc(desc), err
                )
                continue
            shown = value[:_MAX_DESCRIPTOR_BYTES]
            LOGGER.warning(
                "PROBE %s DESC %s = %s", address, _label_desc(desc), shown.hex(" ")
            )


def _log_refusals(
    address: str, verb: str, chars: Sequence[BleakGATTCharacteristic]
) -> None:
    """
    Log each characteristic ``start_notify`` refused, one line at a time.

    A refusal can be the finding rather than the noise: in a live capture one
    of the two notify-capable ``2a4d`` declarations rejected the CCCD write
    while the other accepted it, so "silent" may mean "silent on the one
    declaration we managed to enable".
    """
    for char in chars:
        LOGGER.warning("PROBE %s %s %s", address, verb, _label(char))


async def _async_subscribe_extras(
    conn: _RingConnection,
    address: str,
    on_extra: Callable[[BleakGATTCharacteristic, bytearray], None],
) -> tuple[list[BleakGATTCharacteristic], list[BleakGATTCharacteristic]]:
    """Subscribe to every other notify characteristic; return (got, refused)."""
    extra_chars, refused = await conn.debug_subscribe_extras(on_extra)
    LOGGER.warning(
        "PROBE %s subscribed to %d extra notify characteristic(s): %s",
        address,
        len(extra_chars),
        ", ".join(_label(char) for char in extra_chars) or "none",
    )
    _log_refusals(address, "could not subscribe", refused)
    return extra_chars, refused


async def _async_retry_refused(
    conn: _RingConnection,
    address: str,
    refused: Sequence[BleakGATTCharacteristic],
    on_extra: Callable[[BleakGATTCharacteristic, bytearray], None],
) -> list[BleakGATTCharacteristic]:
    """
    Retry the subscriptions that refused, now that the device is configured.

    A peripheral is allowed to reject a CCCD write while its input source is
    idle and accept it once configured, so the first pass's refusals are not
    necessarily permanent ones - and a report characteristic left
    unsubscribed is exactly how a live device looks silent.
    """
    retried, still_refused = await conn.debug_subscribe_specific(refused, on_extra)
    if retried:
        LOGGER.warning(
            "PROBE %s subscribed after configuring: %s",
            address,
            ", ".join(_label(char) for char in retried),
        )
    _log_refusals(address, "still cannot subscribe", still_refused)
    return retried


async def _async_wait_for_window(
    conn: _RingConnection, address: str, duration_s: float
) -> bool:
    """
    Sleep out the capture window; True when the GATT link dropped instead.

    Ending early on a drop keeps the probe honest: with no link there is
    nothing left to capture, and a minute of silence after a disconnect reads
    like a dead input source.
    """
    sleeper = asyncio.ensure_future(asyncio.sleep(duration_s))
    dropped = asyncio.ensure_future(conn.dead.wait())
    try:
        done, _ = await asyncio.wait(
            {sleeper, dropped},
            timeout=duration_s + 5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for waiter in (sleeper, dropped):
            if not waiter.done():
                waiter.cancel()
    if dropped in done:
        LOGGER.warning(
            "PROBE %s GATT link dropped - ending the probe early",
            address,
        )
        return True
    return False


async def _recheck_hid_descriptors(conn: _RingConnection, address: str) -> None:
    """
    Re-read the HID descriptors once the capture window has closed.

    Doing it at the end as well as the start is what makes one run answer the
    ``appType`` question: if the firmware swaps a gesture's HID usage table
    when configured, the Report Map read after the config frame differs from
    the one read before it, and the two lines sit next to each other in the
    log. If they match while report payloads differ, the map is fixed and only
    the usages inside it move - a single decoder plus a config knob, instead of
    a layout per profile.
    """
    chars = [
        char
        for char in sorted(conn.debug_gatt_chars(), key=_hogp_priority)
        if _hogp_priority(char) == 0 and "read" in char.properties
    ]
    for char in chars:
        try:
            value = await conn.debug_read(char)
        except (TimeoutError, BleakError, OSError) as err:
            LOGGER.warning("PROBE %s RE-READ %s failed: %s", address, _label(char), err)
            continue
        shown = value[:_MAX_READ_BYTES]
        suffix = f" ... ({len(value)} bytes total)" if len(value) > len(shown) else ""
        LOGGER.warning(
            "PROBE %s RE-READ %s = %s%s",
            address,
            _label(char),
            shown.hex(" "),
            suffix,
        )


async def _async_run_probe(  # noqa: PLR0915 - one straight-line probe procedure
    entry: ColmiRingConfigEntry,
    *,
    duration_s: float,
    frames: list[bytearray],
    writes: list[tuple[str, bytearray]],
    subscribe_all: bool,
) -> str:
    address = str(entry.data[CONF_ADDRESS])
    data = entry.runtime_data
    # Checked by the service handler; narrowed again for the type-checker.
    client = data.client
    assert isinstance(client, ColmiRingApiClient)
    coordinator = data.coordinator

    started = time.monotonic()
    lines_logged = 0
    extra_chars: list[BleakGATTCharacteristic] = []
    conn = None
    tally: dict[str, int] = {}

    def note(source: str, packet: bytes) -> None:
        nonlocal lines_logged
        key = f"{source} {packet[:2].hex()}"
        tally[key] = tally.get(key, 0) + 1
        if lines_logged < _MAX_PACKET_LINES:
            lines_logged += 1
            LOGGER.warning(
                "PROBE %s %s +%.3fs %s",
                source,
                address,
                time.monotonic() - started,
                packet.hex(" "),
            )
        elif lines_logged == _MAX_PACKET_LINES:
            lines_logged += 1
            LOGGER.warning(
                "PROBE %s packet log capped at %d lines; the remaining"
                " packets only appear in the end tally",
                address,
                _MAX_PACKET_LINES,
            )

    def on_uart_tx(packet: bytearray) -> None:
        note("RX", packet)

    def on_extra(char: BleakGATTCharacteristic, packet: bytearray) -> None:
        # The handle is part of the label so a report on one of several
        # same-UUID declarations is attributable: on this ring four of them
        # share ``2a4d``, and which one fired is the whole measurement.
        note(f"RX[{char.uuid[:8]}#{char.handle}]", packet)

    def pause_polling() -> timedelta | None:
        """
        Stop the coordinator's polling; return what to restore afterwards.

        ``update_interval = None`` is the coordinator's own documented
        disable (``_schedule_refresh`` returns early on it), and the
        already-scheduled tick is cancelled through the private unsub so no
        poll starts after this returns. The probe's exclusive lease would
        refuse one anyway, but a refused poll is log noise in the middle of
        a capture. Both private calls are deliberate: this module exists to
        poke at internals.
        """
        previous = coordinator.update_interval
        coordinator.update_interval = None
        coordinator._async_unsub_refresh()  # noqa: SLF001
        return previous

    def resume_polling(previous: timedelta | None) -> None:
        """Undo :func:`pause_polling` and re-arm the periodic refresh."""
        coordinator.update_interval = previous
        coordinator._schedule_refresh()  # noqa: SLF001

    saved_interval: timedelta | None = None
    polling_paused = False
    try:
        saved_interval = pause_polling()
        polling_paused = True
        try:
            async with client.debug_link.hold(
                "debug probe", exclusive=True, wait_s=45.0
            ) as conn:
                prev_observer = conn.raw_observer
                conn.raw_observer = on_uart_tx
                try:
                    LOGGER.warning(
                        "PROBE %s link up after %.1fs: listening for %.0fs"
                        " - interact with the ring now",
                        address,
                        time.monotonic() - started,
                        duration_s,
                    )
                    for line in conn.debug_gatt_table():
                        LOGGER.warning("PROBE %s GATT %s", address, line)
                    await _dump_readable_chars(conn, address)
                    await _dump_hid_descriptors(conn, address)
                    refused: list[BleakGATTCharacteristic] = []
                    if subscribe_all:
                        extra_chars, refused = await _async_subscribe_extras(
                            conn, address, on_extra
                        )
                    for frame in frames:
                        LOGGER.warning("PROBE %s TX %s", address, frame.hex(" "))
                        await conn.debug_send(frame)
                        await asyncio.sleep(0.2)
                    await _send_writes(conn, address, writes)
                    if refused:
                        extra_chars.extend(
                            await _async_retry_refused(conn, address, refused, on_extra)
                        )
                    if not await _async_wait_for_window(conn, address, duration_s):
                        # Re-read the HID descriptors now that everything above
                        # has been configured, so one run can say whether the
                        # config frame changed them (see the function).
                        await _recheck_hid_descriptors(conn, address)
                finally:
                    conn.raw_observer = prev_observer
        except (TimeoutError, ColmiRingApiClientError) as err:
            LOGGER.warning(
                "PROBE %s probe ended with an error: %s",
                address,
                err,
            )
    finally:
        if conn is not None:
            await conn.debug_unsubscribe_extras(extra_chars)
        if polling_paused:
            resume_polling(saved_interval)
        summary = ", ".join(f"{key} x{count}" for key, count in tally.items())
        result = (
            f"{address}: captured {sum(tally.values())} packet(s)"
            f" [{summary or 'none'}] in {time.monotonic() - started:.1f}s"
        )
        LOGGER.warning("PROBE %s debug probe finished: %s", address, result)
    return result
