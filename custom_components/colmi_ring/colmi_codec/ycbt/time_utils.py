"""Timestamp helpers for the YCBT protocol.

Yucheng firmware stores timestamps as *seconds since 2000-01-01*, in
wall-clock local time that the firmware interprets as UTC (this is exactly
how the ``setting_time`` command sends the clock). Converting to real Unix
time therefore requires subtracting the device owner's timezone offset,
which is passed in as a parameter so this codec stays free of any
Home-Assistant or host-timezone coupling.
"""

from __future__ import annotations

from .constants import EPOCH_2000_UNIX_SECONDS


def ts2k_to_unix_ms(ts2k: int, tz_offset_sec: int = 0) -> int:
    """Convert a uint32 'seconds since 2000-01-01 (local-as-UTC)' to real Unix ms."""
    local_as_utc_ms = (ts2k + EPOCH_2000_UNIX_SECONDS) * 1000
    return local_as_utc_ms - tz_offset_sec * 1000


def read_ts2k_ms(data: bytes, off: int, tz_offset_sec: int = 0) -> int:
    """Read a 4-byte little-endian ts2k field at ``off`` and convert it."""
    ts2k = (
        (data[off] & 0xFF)
        | ((data[off + 1] & 0xFF) << 8)
        | ((data[off + 2] & 0xFF) << 16)
        | ((data[off + 3] & 0xFF) << 24)
    )
    return ts2k_to_unix_ms(ts2k, tz_offset_sec)
