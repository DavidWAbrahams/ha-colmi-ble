#!/usr/bin/env python3
"""
Regression selftest for issue #59: YCBT 0xFC must be a per-command refusal.

Reproduces, without any radio, the four firmware behaviors this codebase has
to distinguish, by driving the real _YcbtConnection notification path
(_handle_notify -> queues/refusals -> async_init / async_set_monitor_interval
/ async_probe_family) with synthetic reply frames from the real codec:

* the R99 "R11M" ring from issue #59: everything answers 0x00 except
  0x011C (set_blood_pressure_monitor), which answers 0xFC because the
  variant has no BP sensor. Init must be accepted on the first burst (no
  bind-retry spend), the monitor-interval write must still confirm via its
  own opcode echo, and the config probe must pass.
* the owner-tested R88 shape: every Setting command echoes 0x00. Behavior
  must be identical to before the per-command fix: all answered, no
  refusals.
* a genuinely not-bound ring: *every* init command answered with 0xFC, even
  behind the bind write -> init must re-attempt the burst led by a 0x0109
  user-bind write, async_probe_family must raise the "not bound"
  authentication error, and async_set_monitor_interval must fail.
* a wrong-family (silent) ring: no replies at all -> probe must raise the
  communication error, not the authentication one.

Before commit 66bc4ac ("Treat YCBT 0xFC as a per-command refusal, not a
bind verdict") the first scenario failed: a lone 0xFC bumped a shared
rejection counter and the whole burst was treated as refused. If that
regression ever returns, scenario 1 fails.

Run: ``python scripts/_ycbt_refusal_selftest.py``. Bursts run fast here:
the driver swaps ycbt_api's ``asyncio`` for a shim whose ``sleep`` is a
no-op and whose ``wait_for`` clamps reply timeouts, so the whole run
finishes in a second or two instead of paying the real ~3 s bursts and
10 s REPLY_TIMEOUTs.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------- HA stubs
# Same minimal stub strategy as scripts/_ycbt_import_check.py: homeassistant
# and bleak_retry_connector become auto-dummies; the codec, api.py's
# constants/exceptions and the whole of ycbt_api.py stay the real modules.


def _install_ha_stubs() -> None:
    import importlib.abc
    import importlib.machinery
    import types

    class _AutoMeta(type):
        def __getattr__(cls, name):
            if name.startswith("_"):
                raise AttributeError(name)
            return _make(name)

    def _make(name):
        return _AutoMeta(
            name,
            (),
            {
                "__init__": lambda self, *a, **k: None,
                "__getattr__": lambda self, n: None,
            },
        )

    class StubLoader(importlib.abc.Loader):
        def create_module(self, spec):
            mod = types.ModuleType(spec.name)
            mod.__path__ = []
            mod.__getattr__ = lambda name: _make(name)
            return mod

        def exec_module(self, module):
            return None

    class StubFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            root = fullname.split(".", 1)[0]
            if root not in ("homeassistant", "bleak_retry_connector"):
                return None
            return importlib.machinery.ModuleSpec(fullname, StubLoader())

    sys.meta_path.insert(0, StubFinder())

    class Store:
        def __init__(self, hass, version, key, **kwargs):
            self.hass = hass
            self.version = version
            self.key = key

        async def async_load(self):
            return None

        async def async_save(self, data):
            return None

    storage_mod = types.ModuleType("homeassistant.helpers.storage")
    storage_mod.Store = Store
    sys.modules["homeassistant.helpers.storage"] = storage_mod


_install_ha_stubs()

from custom_components.colmi_ring import ycbt_api  # noqa: E402
from custom_components.colmi_ring.api import (  # noqa: E402
    ColmiRingApiClientAuthenticationError,
    ColmiRingApiClientCommunicationError,
)
from custom_components.colmi_ring.colmi_codec.ycbt import (  # noqa: E402
    constants as yc,
)
from custom_components.colmi_ring.colmi_codec.ycbt import packet as yc_pkt  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"

# Every opcode async_init puts on the wire, in burst order (attempt 1).
INIT_BURST_OPS = [
    yc.SETTING_TIME,
    yc.SET_MONITOR_INTERVAL,
    yc.SET_BLOOD_PRESSURE_MONITOR,
    yc.SET_TEMPERATURE_MONITOR,
    yc.ENABLE_BG_SPO2_MONITOR,
    yc.ENABLE_HEALTH_SENSORS,
    yc.GET_DEVICE_NAME,
    yc.GET_DEVICE_INFO,
    yc.GET_POWER_STATISTICS,
    yc.GET_NOW_STEP,
]

# Opcodes this firmware family answers with a generic 1-byte status echo.
SETTING_ECHO_OPS = frozenset(INIT_BURST_OPS) | {yc.SETTING_USER_BIND_ECHO}


class _Char:
    """Stands in for the BleakGATTCharacteristic arg of _handle_notify."""

    uuid = "simulated"


class FakeRing:
    """
    A YCBT firmware simulator: consumes written frames, returns replies.

    ``refuse``: opcodes answered with the 1-byte 0xFC refusal.
    ``silent``: answer nothing at all (a Colmi-family ring ignores these).
    """

    def __init__(self, refuse=frozenset(), silent=False):
        self.refuse = set(refuse)
        self.silent = silent
        self.writes: list[yc_pkt.YcbtPacket] = []

    def on_write(self, packet: yc_pkt.YcbtPacket) -> list[yc_pkt.YcbtPacket]:
        self.writes.append(packet)
        if self.silent:
            return []
        op = packet.data_type
        if op in self.refuse:
            return [yc_pkt.YcbtPacket(op, bytes([yc.REJECTION_BYTE]))]
        if op == yc.GET_DEVICE_NAME:
            # The issue #59 ring reports "R11M" (5231314d00).
            return [yc_pkt.YcbtPacket(op, b"R11M\x00")]
        if op in SETTING_ECHO_OPS:
            return [yc_pkt.YcbtPacket(op, b"\x00")]
        return []


def make_connection(ring: FakeRing, monitor_interval: int = 30):
    """
    A real _YcbtConnection whose transport is the FakeRing.

    Everything above send() - _handle_notify, the opcode queues, the
    refusals set, async_init and the reply bookkeeping it feeds - is the
    production implementation; only the BLE I/O is replaced.
    """
    conn = ycbt_api._YcbtConnection(
        hass=None,
        address=ADDRESS,
        lock=None,
        purpose="selftest",
        monitor_interval_minutes=monitor_interval,
    )

    async def patched_send(packet: yc_pkt.YcbtPacket) -> None:
        for reply in ring.on_write(packet):
            conn._handle_notify(_Char(), bytearray(reply.encode()))

    async def patched_connect():
        return conn

    async def patched_disconnect():
        return None

    conn.send = patched_send
    conn._async_connect = patched_connect
    conn._safe_disconnect = patched_disconnect
    return conn


def make_client(conn) -> ycbt_api.YcbtRingApiClient:
    """A real YcbtRingApiClient whose _connection() hands back `conn`."""
    client = ycbt_api.YcbtRingApiClient(hass=None, ring_address=ADDRESS)
    client._connection = lambda purpose, lock_wait_s: conn
    return client


# --------------------------------------------------------------- scenarios


async def scenario_r11m() -> None:
    """Issue #59: one 0xFC on 0x011C must not poison the whole burst."""
    ring = FakeRing(refuse={yc.SET_BLOOD_PRESSURE_MONITOR})
    conn = make_connection(ring)
    answered, refused = await conn.async_init()

    assert refused == {yc.SET_BLOOD_PRESSURE_MONITOR}, refused
    for op in (
        yc.SETTING_TIME,
        yc.SET_MONITOR_INTERVAL,
        yc.GET_DEVICE_NAME,
        yc.GET_DEVICE_INFO,
        yc.GET_POWER_STATISTICS,
        yc.GET_NOW_STEP,
    ):
        assert op in answered, (op, answered)
    # Accepted on attempt 1: exactly one burst, and the bind retry (0x0109)
    # must not be spent on a ring that answers everything else.
    assert len(ring.writes) == len(INIT_BURST_OPS), [w.data_type for w in ring.writes]
    assert all(w.data_type != yc.SETTING_USER_BIND_ECHO for w in ring.writes), (
        "bind retry fired for a per-command refusal"
    )
    # The refusal frame is bookkeeping, never a queued reply.
    queue = conn._queues.get(yc.SET_BLOOD_PRESSURE_MONITOR)
    assert queue is None or queue.empty()

    # The number-entity write confirms via the 0x010C echo; the 0x011C
    # refusal must not fail it (issue #59's async_set_monitor_interval).
    client = make_client(conn)
    accepted = await client.async_set_monitor_interval(45)
    assert accepted == 45
    assert client._monitor_interval == 45

    # And the config-flow probe passes for this ring.
    await client.async_probe_family()


async def scenario_r88() -> None:
    """The owner-tested R88 shape: all-0x00 echoes, behavior unchanged."""
    ring = FakeRing()
    conn = make_connection(ring)
    answered, refused = await conn.async_init()

    assert refused == set(), refused
    assert answered == set(INIT_BURST_OPS), answered
    assert len(ring.writes) == len(INIT_BURST_OPS)

    client = make_client(conn)
    accepted = await client.async_set_monitor_interval(30)
    assert accepted == 30
    await client.async_probe_family()


async def scenario_not_bound() -> None:
    """Everything refused is still a bind verdict: retry, then auth error."""
    ring = FakeRing(refuse=set(SETTING_ECHO_OPS))
    conn = make_connection(ring)
    answered, refused = await conn.async_init()

    assert answered == set(), answered
    # The last burst's refusals: every command, plus the bind write itself.
    assert refused == set(INIT_BURST_OPS) | {yc.SETTING_USER_BIND_ECHO}, refused
    # Two bursts were spent...
    assert len(ring.writes) == 2 * len(INIT_BURST_OPS) + 1
    # ...and the second was led by the user-bind write (see 9f0d359).
    assert ring.writes[len(INIT_BURST_OPS)].data_type == yc.SETTING_USER_BIND_ECHO, (
        "bind retry not leading the recovery burst"
    )

    client = make_client(conn)
    try:
        await client.async_probe_family()
    except ColmiRingApiClientAuthenticationError as err:
        assert "0xFC" in str(err), str(err)
    else:
        raise AssertionError("not-bound ring passed async_probe_family")

    # An interval write with nothing echoing must fail, not silently pass.
    try:
        await client.async_set_monitor_interval(60)
    except ColmiRingApiClientCommunicationError:
        pass
    else:
        raise AssertionError("interval write passed on a fully refused ring")


async def scenario_silent() -> None:
    """A ring that ignores YCBT frames is the wrong-family/comm case."""
    ring = FakeRing(silent=True)
    conn = make_connection(ring)
    answered, refused = await conn.async_init()
    assert answered == set() and refused == set()

    client = make_client(conn)
    try:
        await client.async_probe_family()
    except ColmiRingApiClientCommunicationError:
        pass
    else:
        raise AssertionError("silent ring raised the wrong error")


# --------------------------------------------------------------- driver


class _FastAsyncio:
    """asyncio shim: no-op sleep, clamped wait_for, everything else real."""

    async def sleep(self, _delay, **kwargs):
        await asyncio.sleep(0)

    async def wait_for(self, future, timeout=None):
        if timeout is not None:
            timeout = min(timeout, 0.25)
        return await asyncio.wait_for(future, timeout)

    def __getattr__(self, name):
        return getattr(asyncio, name)


async def main() -> int:
    ycbt_api.asyncio = _FastAsyncio()
    try:
        await scenario_r11m()
        print("scenario 1 (R99/R11M, 0xFC only on 0x011C): accepted, no retry, OK")
        await scenario_r88()
        print("scenario 2 (R88, all 0x00 echoes): unchanged behavior, OK")
        await scenario_not_bound()
        print("scenario 3 (every command 0xFC): bind retry + auth error, OK")
        await scenario_silent()
        print("scenario 4 (silent ring): comm error, OK")
    finally:
        del ycbt_api.asyncio
    print("ycbt refusal selftest: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
