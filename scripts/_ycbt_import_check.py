#!/usr/bin/env python3
"""
Dev-only: import-check ycbt_api without a full Home Assistant install.

Only stubs the single HA symbol the module imports at load time
(``homeassistant.helpers.storage.Store``); everything else in the import
chain (bleak, bleak_retry_connector, the package's own modules) is real.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


class _AutoMeta(type):
    """Metaclass so dummy classes tolerate any attribute access / subclassing."""

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


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__path__ = []  # make it a package so submodules resolve here too
        # PEP 562: any unknown attribute becomes an auto dummy.
        mod.__getattr__ = lambda name: _make(name)
        return mod

    def exec_module(self, module):
        return None


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root not in ("homeassistant", "bleak_retry_connector"):
            return None
        return importlib.machinery.ModuleSpec(fullname, _StubLoader())


sys.meta_path.insert(0, _StubFinder())


class Store:
    def __init__(self, hass, version, key, **kwargs):
        self.hass = hass
        self.version = version
        self.key = key

    async def async_load(self):
        return None

    async def async_save(self, data):
        return None


# Register the one module whose behavior we assert on, before the finder
# can serve an auto-dummy for it.
_storage_mod = types.ModuleType("homeassistant.helpers.storage")
_storage_mod.Store = Store
sys.modules["homeassistant.helpers.storage"] = _storage_mod

from custom_components.colmi_ring.ycbt_api import (  # noqa: E402
    YcbtRingApiClient,
    _YcbtConnection,
    hrs_bpm,
)

# Quick behavioral spot-checks of the pure helpers while we are here.
assert hrs_bpm(bytes([0x00, 72])) == 72, "8-bit HR"
assert hrs_bpm(bytes([0x01, 0xA0, 0x00])) == 160, "16-bit LE HR"
assert hrs_bpm(bytes([0x04, 60])) is None, "not-worn must be ignored"
assert hrs_bpm(bytes([0x14, 60])) is None, "not-worn with 16-bit flag too"
assert hrs_bpm(b"") is None
assert hrs_bpm(bytes([0x00])) is None, "truncated"

# Constructor smoke (no hass calls happen in __init__ beyond Store stub).
client = YcbtRingApiClient(hass=object(), ring_address="07:35:00:04:6B:81")
assert client._store.key.endswith("07:35:00:04:6b:81")
assert callable(client.async_get_data)
assert callable(client.async_take_heart_rate)
assert callable(client.async_take_spo2)
assert callable(client.async_get_battery)
assert callable(client.async_set_time)
assert callable(client.async_probe_family)
assert hasattr(_YcbtConnection, "async_init")

print("ycbt_api import check: all checks passed")
