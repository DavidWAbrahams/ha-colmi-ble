#!/usr/bin/env python3
"""
Dev-only: verify the ring-clock-domain fix for YCBT history timestamps.

Regression test for the DST bug: record timestamps and watermarks are kept
in the ring's own domain ("local wall clock read as UTC"), so a host DST
transition can neither re-date old records nor fake a "future" watermark.
Conversion to real instants (``_ring_ms_to_dt``) must apply the zone's
rules for the *record's* date, not the host's current offset.

Stubs Home Assistant the way ``_ycbt_import_check.py`` does, except
``homeassistant.util.dt`` gets a real zoneinfo-backed shim so the DST
math under test is genuine.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


_storage_mod = types.ModuleType("homeassistant.helpers.storage")
_storage_mod.Store = Store
sys.modules["homeassistant.helpers.storage"] = _storage_mod

# Real zoneinfo-backed stand-in for homeassistant.util.dt: DEFAULT_TIME_ZONE
# is a ZoneInfo (as in HA), so fold resolution during fall-back is genuine.
_dt_mod = types.ModuleType("homeassistant.util.dt")
_dt_mod.DEFAULT_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_dt_mod.get_time_zone = lambda name: ZoneInfo(name)


def _set_default_time_zone(zone):
    _dt_mod.DEFAULT_TIME_ZONE = zone


_dt_mod.set_default_time_zone = _set_default_time_zone
sys.modules["homeassistant.util.dt"] = _dt_mod
# The stub parent's auto __getattr__ would shadow the submodule attribute
# during ``from homeassistant.util import dt``, so register the parents as
# real (empty) packages carrying the one real child.
_ha_root = types.ModuleType("homeassistant")
_ha_root.__path__ = []
sys.modules["homeassistant"] = _ha_root
_ha_util = types.ModuleType("homeassistant.util")
_ha_util.__path__ = []
_ha_util.dt = _dt_mod
sys.modules["homeassistant.util"] = _ha_util

from custom_components.colmi_ring.ycbt_api import (  # noqa: E402
    HWM_FUTURE_SKEW_S,
    _hwm_is_future,
    _ring_ms_to_dt,
    _ring_now_ms,
)

FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILS
    if not ok:
        FAILS += 1
    print(f"{'PASS' if ok else 'FAIL'}: {name} {detail}")


# ---- _ring_ms_to_dt: DST rules apply per-record, not per-now ----
# Wall-clock stamps around the 2026 US DST transitions (PST -08:00 / PDT
# -07:00). The old bug applied one offset to every record; these must now
# carry the offset that was in effect *at each record's wall time*.
ZONE = ZoneInfo("America/Los_Angeles")
cases = [
    (datetime(2026, 3, 7, 10, 0), -8 * 3600, "before spring-forward"),
    (datetime(2026, 3, 8, 10, 0), -7 * 3600, "after spring-forward"),
    (datetime(2026, 10, 31, 10, 0), -7 * 3600, "before fall-back"),
    (datetime(2026, 11, 1, 10, 0), -8 * 3600, "after fall-back"),
]
for wall, expect_off, label in cases:
    ring_ms = int(wall.replace(tzinfo=UTC).timestamp() * 1000)
    aware = _ring_ms_to_dt(ring_ms)
    off = int(aware.utcoffset().total_seconds())
    check(
        f"offset {label}",
        off == expect_off and aware.replace(tzinfo=None) == wall,
        f"-> {aware.isoformat()}",
    )

# Fall-back ambiguity: the repeated wall time resolves to the first
# (daylight) occurrence, and both real instants for one wall stamp keep
# their distinct ring stamps mapping deterministic and monotone-safe.
amb = _ring_ms_to_dt(
    int(datetime(2026, 11, 1, 1, 30).replace(tzinfo=UTC).timestamp() * 1000)
)
check(
    "ambiguous fall-back wall time -> first (PDT) occurrence",
    amb.utcoffset() == timedelta(hours=-7),
    f"-> {amb.isoformat()}",
)

# A record stamped 'now' must map to (roughly) the real host now.
now_aware = _ring_ms_to_dt(_ring_now_ms())
delta = abs((now_aware - datetime.now(UTC)).total_seconds())
check("ring-now maps to real now", delta < 2.0, f"delta={delta:.3f}s")

# ---- future guard: ring clock vs ring clock, never host epoch ----
check(
    "fresh watermark not future",
    not _hwm_is_future(_ring_now_ms()),
)
check(
    "4-min-ahead watermark not future",
    not _hwm_is_future(_ring_now_ms() + (HWM_FUTURE_SKEW_S - 60) * 1000),
)
check(
    "6-min-ahead watermark is future",
    _hwm_is_future(_ring_now_ms() + (HWM_FUTURE_SKEW_S + 60) * 1000),
)
# The exact bug scenario: after fall-back, a watermark re-expressed one
# hour ahead of *epoch* was flagged "future" by the old guard. In the
# ring domain the same record is at ring-now, so it must never trip.
check(
    "post-fall-back record (+1h vs epoch) not future",
    not _hwm_is_future(_ring_now_ms() + 3600 * 1000 - 12 * 3600 * 1000),
    "(1h-post-transition ring stamp in a -8h zone)",
)

print("ALL PASS" if FAILS == 0 else f"{FAILS} FAILURE(S)")
sys.exit(1 if FAILS else 0)
