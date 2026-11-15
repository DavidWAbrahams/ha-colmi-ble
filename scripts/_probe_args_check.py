#!/usr/bin/env python3
"""
Dev-only: check the debug probe's argument parsing without Home Assistant.

``colmi_ring.debug_packet_probe`` takes list-of-string fields, but what the
handler actually receives depends on how the call was authored: a YAML list
arrives as a list, a form built from an object selector arrives as a mapping,
a pasted JSON array arrives as a string. This stubs the HA import chain the
way ``_ycbt_import_check.py`` does and asserts every shape lands on the same
parsed result, so nobody has to guess the accepted syntax at 1am.
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

from custom_components.colmi_ring import debug_probe  # noqa: E402

CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ok  {name}{f' ({detail})' if detail else ''}")


def raises(func, value) -> bool:
    """True when ``func(value)`` rejects the input instead of guessing."""
    try:
        func(value)
    except (TypeError, ValueError):
        return True
    return False


def frames_of(value) -> list[str]:
    return [bytes(packet).hex(" ") for packet in debug_probe._parse_frames(value)]


def writes_of(value) -> list[str]:
    return [
        f"{uuid}={bytes(payload).hex(' ')}"
        for uuid, payload in debug_probe._parse_writes(value)
    ]


print("frames field shapes:")
FRAME = "3B 02 00 01 02 00 00 00 00 00 00 00 00 00 00 40"
EXPECTED = ["3b 02 00 01 02 00 00 00 00 00 00 00 00 00 00 40"]
check("omitted (None)", frames_of(None) == [])
check("YAML list", frames_of([FRAME]) == EXPECTED, str(frames_of([FRAME])))
check("selector mapping", frames_of({"0": FRAME}) == EXPECTED)
check("JSON string", frames_of(f'["{FRAME}"]') == EXPECTED)
check("bare hex string", frames_of(FRAME) == EXPECTED)
check("newline separated", frames_of(f"{FRAME}\n\n{FRAME}") == EXPECTED * 2)
check(
    "colons tolerated",
    frames_of(["3B:02:00:01:02:00:00:00:00:00:00:00:00:00:00:40"]) == EXPECTED,
)
check("bad hex rejected", raises(debug_probe._parse_frames, ["zz"]))
check("over-long frame rejected", raises(debug_probe._parse_frames, ["AA" * 32]))

print("writes field shapes:")
BASE = "00002a4c-0000-1000-8000-00805f9b34fb"
REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
check("omitted (None)", writes_of(None) == [])
check(
    "YAML list",
    writes_of(["2a4c = 01"]) == [f"{BASE}=01"],
    str(writes_of(["2a4c = 01"])),
)
check("selector mapping", writes_of({"0": "2a4c = 01"}) == [f"{BASE}=01"])
check("JSON string", writes_of('["2a4c = 01"]') == [f"{BASE}=01"])
check(
    "full 128-bit UUID",
    writes_of([f"{BASE} = 01"]) == [f"{BASE}=01"],
)
check("multi-byte payload", writes_of(["2a4d = 01 02"]) == [REPORT + "=01 02"])
check("missing '=' rejected", raises(debug_probe._parse_writes, ["2a4c 01"]))
check("empty bytes rejected", raises(debug_probe._parse_writes, ["2a4c ="]))
check("non-hex rejected", raises(debug_probe._parse_writes, ["2a4c = zz"]))
check("wrong type rejected", raises(debug_probe._parse_writes, 42))

print("services.yaml keeps the list fields selector-free:")
SERVICES = (_ROOT / "custom_components" / "colmi_ring" / "services.yaml").read_text(
    encoding="utf-8"
)
NEXT_FIELD = {"frames": "\n    writes:", "writes": "\n    subscribe_all:"}
for field in ("frames", "writes"):
    block = SERVICES.split(f"\n    {field}:")[1].split(NEXT_FIELD[field])[0]
    check(f"{field}: no selector", "selector:" not in block)
    check(f"{field}: documented", "description" in block and "example" in block)

print(f"debug probe argument check: {CHECKS} checks passed")
