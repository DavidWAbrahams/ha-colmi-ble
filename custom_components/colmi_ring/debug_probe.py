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
    from datetime import timedelta

    from bleak.backends.characteristic import BleakGATTCharacteristic
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
_MAX_READ_CHARS = 12
_MAX_READ_BYTES = 300

# How much of a raw write to echo into the log before truncating it.
_MAX_WRITE_LOG_BYTES = 32

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): str,
        vol.Optional("address"): str,
        vol.Optional("duration_s", default=60.0): vol.All(
            vol.Coerce(float), vol.Range(min=5.0, max=900.0)
        ),
        # Each frame is plain hex, e.g. "3b 02 01 00 ..." (spaces optional).
        vol.Optional("frames", default=list): [str],
        # Raw writes to any characteristic: "UUID = hex bytes", where UUID may
        # be a Bluetooth SIG 16-bit assigned number ("2a4c") or a full 128-bit
        # UUID. Needed to drive paths the UART codec does not know about.
        vol.Optional("writes", default=list): [str],
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
        frames = _parse_frames(call.data["frames"])
        writes = _parse_writes(call.data["writes"])
    except ValueError as err:
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


def _parse_frames(frames: list[str]) -> list[bytearray]:
    parsed: list[bytearray] = []
    for index, frame in enumerate(frames):
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


def _parse_writes(writes: list[str]) -> list[tuple[str, bytearray]]:
    """
    Parse ``"UUID = hex bytes"`` write requests.

    A 4-digit UUID is a Bluetooth SIG assigned number and is expanded to the
    standard base UUID, because typing the whole 128-bit form for ``2a4c`` is
    the kind of friction that stops a probe from being run.
    """
    parsed: list[tuple[str, bytearray]] = []
    for index, item in enumerate(writes):
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
            await conn.debug_write(uuid, payload)
        except (TimeoutError, BleakError, OSError) as err:
            LOGGER.warning("PROBE %s TX[%s] failed: %s", address, uuid[:8], err)
        await asyncio.sleep(0.2)


async def _dump_readable_chars(conn: _RingConnection, address: str) -> None:
    """
    Log every readable characteristic's value.

    Static descriptors answer questions traffic cannot: the Bluetooth HID
    Report Map (``2a4b``) states how a ring's input reports are laid out,
    and the HID Information (``2a4a``) whether it implements HID over GATT
    at all - i.e. whether touch events can reach a BLE host. Reads are
    capped so an unexpectedly huge descriptor cannot spam the log, and each
    is guarded because some firmwares reject individual reads.
    """
    for uuid in conn.debug_readable_chars()[:_MAX_READ_CHARS]:
        try:
            value = await conn.debug_read(uuid)
        except (TimeoutError, BleakError, OSError) as err:
            LOGGER.warning("PROBE %s READ %s failed: %s", address, uuid, err)
            continue
        shown = value[:_MAX_READ_BYTES]
        suffix = f" ... ({len(value)} bytes total)" if len(value) > len(shown) else ""
        LOGGER.warning("PROBE %s READ %s = %s%s", address, uuid, shown.hex(" "), suffix)


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
    extra_uuids: list[str] = []
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
        note(f"RX[{char.uuid[:8]}]", packet)

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
                    if subscribe_all:
                        extra_uuids = await conn.debug_subscribe_extras(on_extra)
                        LOGGER.warning(
                            "PROBE %s subscribed to %d extra notify"
                            " characteristic(s): %s",
                            address,
                            len(extra_uuids),
                            ", ".join(extra_uuids) or "none",
                        )
                    for frame in frames:
                        LOGGER.warning("PROBE %s TX %s", address, frame.hex(" "))
                        await conn.debug_send(frame)
                        await asyncio.sleep(0.2)
                    await _send_writes(conn, address, writes)
                    # End early if the ring drops the link; the probe is
                    # worthless once there is nothing to capture from.
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
            await conn.debug_unsubscribe_extras(extra_uuids)
        if polling_paused:
            resume_polling(saved_interval)
        summary = ", ".join(f"{key} x{count}" for key, count in tally.items())
        result = (
            f"{address}: captured {sum(tally.values())} packet(s)"
            f" [{summary or 'none'}] in {time.monotonic() - started:.1f}s"
        )
        LOGGER.warning("PROBE %s debug probe finished: %s", address, result)
    return result
