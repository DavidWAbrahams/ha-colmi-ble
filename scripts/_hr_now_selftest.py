#!/usr/bin/env python3
"""
_hr_now_selftest.py

Throwaway validation for colmi_codec.hr: for *today's* log, the
``heart_rates`` property zeroes the day's future slots, but its boundary
math is off by one slot. It computes the first zeroed slot as
``minutes_so_far(now) // range``, and ``minutes_so_far`` returns
"minutes + 1", so the slot that has *already started* (and can already
hold the ring's newest sample - on R08 the sample lands at the slot's
opening boundary) is zeroed for all but the slot's last minute.

api.py learned this the hard way and applies the ``- 1`` correction when
clamping how many slots count as filled ("minutes_so_far() is
'minutes + 1', so subtract it back before dividing"); the parser never
got the same correction, so the freshest sample is erased from the log
before api.py ever sees it - delaying the HR sensor by a whole grid
interval (up to an hour on a 60-min R08) and skewing the off-finger
gap check.

These vectors freeze the clock with a datetime subclass, so the check is
deterministic. Run from the repo root:

    python scripts/_hr_now_selftest.py
"""

from __future__ import annotations

import struct
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "colmi_ring"))

from colmi_codec import date_utils, hr  # noqa: E402
from colmi_codec.packet import checksum  # noqa: E402


class FrozenDatetime(datetime):
    """datetime that answers now() from a fixed instant (constructor intact)."""

    frozen: datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN202
        return cls.frozen.astimezone(tz) if tz is not None else cls.frozen


def pkt(sub_type: int, body: list[int]) -> bytearray:
    """Build a 16-byte 0x15 packet: cmd, sub_type, body..., pad, crc."""
    p = bytearray(16)
    p[0] = hr.CMD_READ_HEART_RATE
    p[1] = sub_type
    for i, b in enumerate(body):
        p[2 + i] = b
    p[15] = checksum(p)
    return p


def first_data_packet(ts: datetime, values: list[int]) -> bytearray:
    """First data packet: 4-byte LE timestamp in bytes 2..5, 9 values in 6..14."""
    assert len(values) == 9
    body = list(struct.pack("<l", int(ts.timestamp()))) + values
    return pkt(0x01, body)


def data_packet(sub: int, values: list[int]) -> bytearray:
    """Data packets after the first carry 13 values in bytes 2..14."""
    assert len(values) == 13
    return pkt(sub, values)


def freeze(now: datetime) -> datetime:
    """Pin hr.py's and date_utils' clocks to ``now`` for one check."""
    FrozenDatetime.frozen = now
    hr.datetime = FrozenDatetime  # type: ignore[attr-defined]
    date_utils.now = lambda: now  # type: ignore[assignment]
    return now


def log_filled_today(
    now: datetime, grid: int, samples: dict[int, int]
) -> hr.HeartRateLog:
    """
    Drive a today-anchored log with given per-slot samples to completion.

    ``samples`` maps slot index -> bpm. Slots 0..8 ride the timestamp
    packet, the rest go into following 13-value packets; the burst is
    sized to just cover the highest sample.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    parser = hr.HeartRateLogParser()

    top = max(samples, default=0)
    n_data = 1 if top <= 8 else (top - 9) // 13 + 2  # data packets incl. sub 1
    size = 1 + n_data
    assert size - 1 >= 1

    assert parser.parse(pkt(0x00, [size, grid])) is None
    first = [samples.get(i, 0) for i in range(9)]
    out = parser.parse(first_data_packet(midnight, first))
    if isinstance(out, hr.HeartRateLog):
        return out
    for sub in range(2, size):
        vals = [samples.get(9 + (sub - 2) * 13 + i, 0) for i in range(13)]
        out = parser.parse(data_packet(sub, vals))
        if out is not None:
            assert isinstance(out, hr.HeartRateLog), f"parser returned {out!r}"
            return out
    raise AssertionError("log never completed")


def check_current_slot_sample_survives() -> None:
    """The in-progress slot (just past its boundary) must not be zeroed."""
    # 14:37 on a 60-min grid: slot 14 started at 14:00 and already holds
    # the ring's 14:00 sample. Slot 13 holds the older 13:00 sample.
    now = freeze(datetime.now(tz=UTC).replace(hour=14, minute=37, second=0))
    log = log_filled_today(now, grid=60, samples={13: 70, 14: 71})

    hr_rates = log.heart_rates
    assert hr_rates[13] == 70, f"13:00 sample lost: slot13={hr_rates[13]}"
    assert hr_rates[14] == 71, (
        "14:00 sample erased by the future-slot zeroing: "
        f"slot14={hr_rates[14]} (expected 71)"
    )
    # Slots that have genuinely not started must still be zeroed.
    assert all(v == 0 for v in hr_rates[15:]), (
        f"not-yet-started slots not zeroed: {hr_rates[15:18]}"
    )
    print("ok  current-slot sample survives the is-today future zeroing")


def check_midnight_boundary() -> None:
    """At 00:00 the day's first slot has started; its sample must survive."""
    # 30 seconds past midnight: minutes_so_far() = 0 + 1, so the buggy
    # // divides 1 by the grid and zeroes slot 0 too - the whole day.
    now = freeze(datetime.now(tz=UTC).replace(hour=0, minute=0, second=0))
    # Pin to 00:00:30 by freezing a rounded-up instant two ways.
    midnight = now.replace(hour=0, minute=0, second=30, microsecond=0)
    now = freeze(midnight)
    log = log_filled_today(now, grid=60, samples={0: 65})

    hr_rates = log.heart_rates
    assert hr_rates[0] == 65, (
        "00:00 sample erased at midnight: slot0="
        f"{hr_rates[0]} (expected 65); everything: {hr_rates[:3]}"
    )
    assert all(v == 0 for v in hr_rates[1:]), "future slots must stay zero"
    print("ok  just-past-midnight sample survives the is-today future zeroing")


if __name__ == "__main__":
    check_current_slot_sample_survives()
    check_midnight_boundary()
    print("all hr today selftests passed")
