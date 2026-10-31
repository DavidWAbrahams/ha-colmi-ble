#!/usr/bin/env python3
"""Phase 0 discovery probe for the Colmi ring accelerometer RAW stream.

Dev-only tool; not part of the integration. It talks to the ring with plain
``bleak`` from this workstation, following the RAW-mode protocol published by
``mrfloydst/smartringmidi``, and answers the four questions that block the
gesture implementation:

  1. How fast does the ring actually stream position packets (Hz)?  That number
     decides whether a clap can be derived from the stream at all.
  2. Which byte offsets carry the axes, and are the low bits parked in the
     *low* nibble of the following byte (what the demo masks with ``& 0xf``) or
     the high nibble (what other reverse-engineerings assume)?
  3. Does the decoded vector sit at ~1 g at rest (this single check validates
     offsets, scale and sign together), and which axis/sign flips on a
     deliberate hand raise?
  4. Does the ring's on-device "camera button" gesture (``02 04`` / ``02 06``)
     emit a packet we could use as a battery-free clap detector - and does it
     still do so *while* RAW mode is streaming, or do the two claim the same
     accelerometer? The gesture is therefore armed twice: once on its own, and
     once on top of an open stream.

The ring serves a single central. Pause or disable the Home Assistant config
entry (and keep the ring close to this machine's Bluetooth adapter) before
running, then::

    python scripts/_accel_probe.py

A JSON-lines capture of every packet is written for offline replay::

    python scripts/_accel_probe.py --capture testdata/accel-capture.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bleak import BleakClient

_ROOT = Path(__file__).resolve().parents[1]

RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEFAULT_ADDRESS = "78:14:12:A0:0F:8C"

CMD_RAW_MODE = 0xA1
CMD_CAMERA_GESTURE = 0x02
TYPE_POSITION = 3


def _load_make_packet():
    """Load ``make_packet`` straight from the codec file.

    Loaded by path rather than by package import so the probe does not execute
    the integration's ``__init__`` (which imports Home Assistant).
    """
    path = _ROOT / "custom_components" / "colmi_ring" / "colmi_codec" / "packet.py"
    spec = importlib.util.spec_from_file_location("colmi_packet", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_packet


make_packet = _load_make_packet()


# ---------------------------------------------------------------- decoding


def int12(value: int) -> int:
    """Sign-extend a 12-bit two's-complement value."""
    return value - 4096 if value > 2047 else value


def decode_axes(
    data: bytes, *, low_bits_high: bool
) -> tuple[float, float, float] | None:
    """Return ``(gx, gy, gz)`` in g, or None if this is not a position packet.

    Each axis is a 12-bit big-endian value split over two bytes, in wire order
    **Y, Z, X**. The reference demo masks the low bits from the *low* nibble of
    the following byte; other write-ups use the high nibble. Both variants are
    supported so the probe can report which one puts the ring at ~1 g at rest.
    """
    if len(data) < 8:
        return None

    def pair(hi: int, lo: int) -> float:
        low = (lo >> 4) if low_bits_high else lo
        raw = ((hi << 4) | (low & 0xF)) & 0xFFF
        return (int12(raw) / 2048.0) * 4.0

    gy = pair(data[2], data[3])
    gz = pair(data[4], data[5])
    gx = pair(data[6], data[7])
    return gx, gy, gz


def magnitude(axes: tuple[float, float, float]) -> float:
    return math.sqrt(sum(value * value for value in axes))


# ---------------------------------------------------------------- capture


@dataclass
class Packet:
    """One notification, timestamped on the monotonic clock."""

    t: float
    data: bytes
    char: str


@dataclass
class Capture:
    """Every packet seen, plus named time markers for each test phase."""

    packets: list[Packet] = field(default_factory=list)
    marks: list[tuple[float, str]] = field(default_factory=list)

    def note(self, label: str) -> None:
        self.marks.append((time.monotonic(), label))

    def span(self, label: str) -> list[Packet]:
        """Packets recorded between ``label`` and the next marker."""
        ordered = sorted(self.marks)
        for index, (_, name) in enumerate(ordered):
            if name != label:
                continue
            start = ordered[index][0]
            end = (
                ordered[index + 1][0]
                if index + 1 < len(ordered)
                else time.monotonic()
            )
            return [p for p in self.packets if start <= p.t < end]
        return []

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for mark_t, label in sorted(self.marks):
                handle.write(json.dumps({"t": mark_t, "mark": label}) + "\n")
            for pkt in self.packets:
                handle.write(
                    json.dumps(
                        {"t": pkt.t, "hex": pkt.data.hex(), "char": pkt.char}
                    )
                    + "\n"
                )


# ---------------------------------------------------------------- transport


def _writable(char) -> bool:
    props = char.properties
    return any(
        name in props for name in ("write", "write-without-response", "write-signed")
    )


def _notifiable(char) -> bool:
    return "notify" in char.properties or "indicate" in char.properties


def pick_characteristics(client: BleakClient) -> tuple[Any, list[Any]]:
    """Return the write characteristic and every notifiable one.

    Prefers the vendor UART UUIDs the integration already uses; otherwise
    subscribes to all notifiable characteristics, as the reference demo does,
    so a motion notification on an unexpected characteristic is still caught.
    """
    write_char = None
    notify_chars: list[Any] = []
    for service in client.services:
        for char in service.characteristics:
            if _writable(char):
                if write_char is None or RX_UUID in char.uuid.lower():
                    write_char = char
            if _notifiable(char):
                notify_chars.append(char)
    preferred = [c for c in notify_chars if TX_UUID in c.uuid.lower()]
    ordered = preferred + [c for c in notify_chars if c not in preferred]
    return write_char, ordered


async def send(client: BleakClient, char: Any, packet: bytearray) -> None:
    print(f"  >> {packet.hex(' ')}")
    await client.write_gatt_char(char, packet, response=False)


# ---------------------------------------------------------------- phases


async def phase(capture: Capture, label: str, seconds: int, instruction: str) -> None:
    """Mark a named window and ask the user to perform the matching action."""
    print(f"\n=== {label}: {instruction} ({seconds}s) ===", flush=True)
    capture.note(label)
    await asyncio.sleep(seconds)


async def run_stream_phases(client: BleakClient, capture: Capture) -> None:
    """The scripted gesture sequence, with the RAW stream open."""
    await phase(capture, "still", 10, "Hold the hand completely still")
    await phase(
        capture,
        "raise_lower",
        20,
        "Rapidly raise the hand and lower it again, repeatedly, "
        "with ~2s pauses between",
    )
    await phase(capture, "clap", 15, "Clap repeatedly, with ~2s pauses between")
    await phase(
        capture,
        "raise_still_lower",
        12,
        "Hold still, raise and hold, then lower and hold still",
    )


async def try_camera_gesture(
    client: BleakClient, write_char: Any, capture: Capture, label: str, heading: str
) -> None:
    """Toggle the ring's on-device camera-button gesture and watch for packets.

    ``label`` names the phase pair in the capture, so the same test can run with
    RAW mode off and again with it on; ``report_camera_gesture`` compares the two.
    The disable window is not a throwaway: packets during it mean the ring
    reports gestures whether or not it was asked, so a count in the enable window
    alone would prove nothing.
    """
    print(f"\n--- {heading} ---")
    await send(
        client, write_char, make_packet(CMD_CAMERA_GESTURE, bytearray([4]))
    )
    await phase(capture, f"{label}_on", 12, "Clap repeatedly")
    await send(
        client, write_char, make_packet(CMD_CAMERA_GESTURE, bytearray([6]))
    )
    await phase(capture, f"{label}_off", 6, "Clap again (gesture disabled)")


# ---------------------------------------------------------------- reporting


def packet_types(capture: Capture) -> Counter[tuple[int, int, int]]:
    counts: Counter[tuple[int, int, int]] = Counter()
    for pkt in capture.packets:
        if len(pkt.data) >= 2:
            counts[(pkt.data[0], pkt.data[1], len(pkt.data))] += 1
    return counts


def report_types(capture: Capture) -> None:
    print("\n## Packet types seen (byte0, byte1, length) -> count")
    types = packet_types(capture)
    if not types:
        print("  (none - the ring sent nothing)")
        return
    for (b0, b1, length), count in types.most_common():
        print(f"  0x{b0:02X} 0x{b1:02X} len={length:2d} -> {count}")


def report_rate(capture: Capture, type_filter: tuple[int, int] | None) -> list[Packet]:
    """Print the stream rate for the dominant packet type; return those packets."""
    picked = [
        pkt
        for pkt in capture.packets
        if type_filter is None
        or (len(pkt.data) >= 2 and (pkt.data[0], pkt.data[1]) == type_filter)
    ]
    print("\n## Stream rate")
    if len(picked) < 2:
        print(f"  only {len(picked)} matching packet(s); cannot compute a rate")
        return picked
    span = picked[-1].t - picked[0].t
    hz = (len(picked) - 1) / span if span > 0 else float("inf")
    gaps = [b.t - a.t for a, b in zip(picked, picked[1:], strict=True)]
    print(
        f"  {len(picked)} packets over {span:.1f}s"
        f" = {hz:.1f} Hz (median gap {statistics.median(gaps) * 1000:.0f} ms,"
        f" min {min(gaps) * 1000:.0f} ms, max {max(gaps) * 1000:.0f} ms)"
    )
    return picked


def report_variance(packets: list[Packet]) -> None:
    """Per-byte variance isolates which offsets actually carry data."""
    print("\n## Per-byte variance (bytes whose values change)")
    if not packets:
        print("  (no packets)")
        return
    width = max(len(pkt.data) for pkt in packets)
    changed: list[int] = []
    for index in range(width):
        values = [pkt.data[index] for pkt in packets if len(pkt.data) > index]
        if len(set(values)) > 1:
            changed.append(index)
            spread = statistics.pstdev(values) if len(values) > 1 else 0.0
            print(
                f"  byte[{index}] min=0x{min(values):02X} max=0x{max(values):02X}"
                f" stdev={spread:6.1f}"
            )
    if not changed:
        print("  every byte is constant - these are not motion samples")


def report_decode(capture: Capture, packets: list[Packet]) -> None:
    """Check the ~1 g-at-rest hypothesis under both nibble assumptions."""
    print("\n## Decode check (rest magnitude should be ~1.00 g)")
    labelled = _in_span(capture, "still", packets) or packets
    for low_bits_high in (False, True):
        label = "high nibble of next byte" if low_bits_high else "low nibble"
        mags = []
        for pkt in labelled:
            axes = decode_axes(pkt.data, low_bits_high=low_bits_high)
            if axes is None:
                continue
            mags.append(magnitude(axes))
        if not mags:
            print(f"  ({label}): no decodable packets")
            continue
        mean = statistics.fmean(mags)
        spread = statistics.pstdev(mags) if len(mags) > 1 else 0.0
        verdict = "OK" if 0.85 <= mean <= 1.15 else "wrong"
        print(
            f"  mask from {label:25s}: |a| = {mean:.3f} g +/- {spread:.3f}"
            f"  -> {verdict}"
        )


def _phase_window(capture: Capture, label: str) -> tuple[float, float] | None:
    """The time span of a named phase, ending at the next marker (or now)."""
    ordered = sorted(capture.marks)
    for index, (mark_t, name) in enumerate(ordered):
        if name != label:
            continue
        end = (
            ordered[index + 1][0] if index + 1 < len(ordered) else time.monotonic()
        )
        return (mark_t, end)
    return None


def _in_span(capture: Capture, label: str, packets: list[Packet]) -> list[Packet]:
    """The already-filtered sample packets that fall inside a named phase.

    ``packets`` is the dominant-type subset, so command replies and other
    chatter never get decoded as motion samples.
    """
    window = _phase_window(capture, label)
    if window is None:
        return []
    start, end = window
    return [pkt for pkt in packets if start <= pkt.t < end]


def _decoded(packet: Packet) -> tuple[float, float, float] | None:
    return decode_axes(packet.data, low_bits_high=False)


def report_gestures(capture: Capture, packets: list[Packet]) -> None:
    """Which axis and sign distinguishes a raise from a lower, and the clap spike."""
    print("\n## Gesture signatures (per phase, mean axis in g)")
    still = _phase_means(capture, "still", packets)
    for label in ("still", "raise_still_lower", "clap", "gesture_with_raw_on"):
        means = _phase_means(capture, label, packets)
        if means is None:
            continue
        text = "  ".join(
            f"{axis}={value:+.2f}" for axis, value in zip("xyz", means, strict=True)
        )
        print(f"  {label:20s} {text}")
        if label == "clap" and still is not None:
            deltas = [m - s for m, s in zip(means, still, strict=True)]
            print(
                f"  {'delta vs still':20s} "
                + "  ".join(
                    f"{axis}={value:+.2f}"
                    for axis, value in zip("xyz", deltas, strict=True)
                )
            )
    _report_peak(capture, "clap", packets)
    _report_axis_extremes(capture, "raise_still_lower", packets)


def _phase_means(
    capture: Capture, label: str, packets: list[Packet]
) -> tuple[float, float, float] | None:
    values = [
        axes
        for pkt in _in_span(capture, label, packets)
        if (axes := _decoded(pkt)) is not None
    ]
    if not values:
        return None
    return tuple(statistics.fmean(col) for col in zip(*values, strict=True))  # type: ignore[return-value]


def _report_peak(capture: Capture, label: str, packets: list[Packet]) -> None:
    peaks = sorted(
        (
            (magnitude(axes), pkt.t)
            for pkt in _in_span(capture, label, packets)
            if (axes := _decoded(pkt)) is not None
        ),
        reverse=True,
    )
    if not peaks:
        return
    print("\n## Clap candidates (top magnitudes during the clap phase)")
    for mag, when in peaks[:5]:
        print(f"  {mag:5.2f} g at t={when:.3f}")


def _report_axis_extremes(
    capture: Capture, label: str, packets: list[Packet]
) -> None:
    axes = [
        decoded
        for pkt in _in_span(capture, label, packets)
        if (decoded := _decoded(pkt)) is not None
    ]
    if not axes:
        return
    print("\n## Raise/lower candidates (per-axis min..max during the hold phase)")
    for index, name in enumerate("xyz"):
        column = [a[index] for a in axes]
        print(f"  {name}: {min(column):+.2f} .. {max(column):+.2f} g")


def _gesture_packets(capture: Capture, label: str, suffix: str) -> list[Packet]:
    """Packets on the gesture command byte during one phase window."""
    window = _phase_window(capture, f"{label}{suffix}")
    if window is None:
        return []
    start, end = window
    return [
        pkt
        for pkt in capture.packets
        if pkt.data and pkt.data[0] == CMD_CAMERA_GESTURE and start <= pkt.t < end
    ]


_GESTURE_PHASES: tuple[tuple[str, str], ...] = (
    ("gesture_alone", "RAW off"),
    ("gesture_with_raw", "RAW on"),
)


def report_camera_gesture(capture: Capture) -> None:
    """Whether ``02 04`` is answered, and whether RAW mode suppresses it.

    Home Assistant used to arm the camera-button gesture only *while* RAW
    streaming ran, so a firmware that feeds both from one accelerometer pipeline
    looked like a firmware with no clap at all. The two are therefore tested
    separately here, and that test is still worth running on an unknown ring:
    Home Assistant's `Clap Detection` switch arms ``02 04`` with RAW off, so the
    ``RAW off`` column below is what predicts whether that switch can work, and
    the ``RAW on`` column is why the two switches refuse each other.
    """
    print("\n## Camera-button gesture, on its own and under RAW mode")
    counts: dict[str, int] = {}
    rows: list[str] = []
    for label, note in _GESTURE_PHASES:
        if _phase_window(capture, f"{label}_on") is None:
            continue
        on = _gesture_packets(capture, label, "_on")
        off = _gesture_packets(capture, label, "_off")
        counts[label] = len(on)
        rows.append(
            f"  {note:9s} 0x02 packets: {len(on)} while enabled,"
            f" {len(off)} while disabled"
        )
    if not rows:
        print("  (gesture phases not reached)")
        return
    print("\n".join(rows))
    if "gesture_with_raw" not in counts:
        # A run that died before the second phase would otherwise be reported as
        # "no gesture at all", which is a statement about the firmware that the
        # capture does not actually support.
        print(
            "\n  Verdict: inconclusive - the run ended before the gesture was armed"
            " a second time under RAW mode, so the two were never compared."
        )
        return

    alone = counts.get("gesture_alone", 0)
    with_raw = counts["gesture_with_raw"]
    print("\n  Verdict:")
    if alone and with_raw:
        print(
            "    Camera mode CONFIRMED, and it coexists with RAW mode: the ring"
            " answers 02 04 either way, so the Clap Detection switch can report"
            " claps on this firmware (Motion Stream is then a separate, optional"
            " feature)."
        )
    elif alone and not with_raw:
        print(
            "    Camera mode CONFIRMED, but ONLY while RAW mode is off: the two are"
            " mutually exclusive. Use the Clap Detection switch with the Motion"
            " Stream switch off - which is why those two switches refuse each other"
            " in Home Assistant rather than both running."
        )
    elif not alone and not with_raw:
        print(
            "    No 0x02 packet either way, so this firmware appears to have no"
            " camera-button gesture at all: the Clap Detection switch will turn"
            " itself off, and a clap could only be derived from a fast enough RAW"
            " stream."
        )
    else:
        print(
            "    0x02 packets appeared only under RAW mode, which is the wrong way"
            " round; re-run in case a clap landed outside its window."
        )


def report(capture: Capture) -> None:
    report_types(capture)
    dominant = _dominant_type(capture)
    packets = report_rate(capture, dominant)
    report_variance(packets)
    report_decode(capture, packets)
    report_gestures(capture, packets)
    report_camera_gesture(capture)


def _dominant_type(capture: Capture) -> tuple[int, int] | None:
    counts: Counter[tuple[int, int]] = Counter(
        (pkt.data[0], pkt.data[1])
        for pkt in capture.packets
        if len(pkt.data) >= 2
    )
    if not counts:
        return None
    (best, _), _ = counts.most_common(1)[0]
    return best


# ---------------------------------------------------------------- main


async def probe(address: str, passive_seconds: int, capture: Capture) -> None:
    """Connect, listen, stream, and report."""
    print(f"Connecting to {address} ...")
    async with BleakClient(address, timeout=30.0) as client:
        write_char, notify_chars = pick_characteristics(client)
        if write_char is None:
            raise SystemExit("no writable characteristic found; is HA holding the ring?")
        print(f"  write: {write_char.uuid}")
        for char in notify_chars:
            print(f"  listen: {char.uuid}")

        def on_notify(char, data: bytearray) -> None:
            capture.packets.append(
                Packet(t=time.monotonic(), data=bytes(data), char=char.uuid)
            )

        for char in notify_chars:
            await client.start_notify(char, on_notify)

        await phase(
            capture, "passive", passive_seconds, "Listening (send nothing yet)"
        )
        # The gesture is tested *before* RAW mode is enabled as well as during
        # it. Home Assistant only ever arms it on top of a running stream, so if
        # the firmware routes both features through one accelerometer pipeline
        # then the stream is what silences the gesture - indistinguishable, from
        # HA's side, from a firmware that has no gesture at all.
        await try_camera_gesture(
            client,
            write_char,
            capture,
            "gesture_alone",
            "camera-button gesture alone, RAW mode OFF (are the two exclusive?)",
        )
        print("\n--- enabling RAW mode ---")
        await send(client, write_char, make_packet(CMD_RAW_MODE, bytearray([4, 4])))
        try:
            await run_stream_phases(client, capture)
            await try_camera_gesture(
                client,
                write_char,
                capture,
                "gesture_with_raw",
                "camera-button gesture over RAW mode ON (as Home Assistant runs it)",
            )
        finally:
            print("\n--- disabling RAW mode ---")
            await send(client, write_char, make_packet(CMD_RAW_MODE, bytearray([2])))
            await asyncio.sleep(1.0)
        for char in notify_chars:
            try:
                await client.stop_notify(char)
            except Exception:  # noqa: BLE001
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--address", default=DEFAULT_ADDRESS)
    parser.add_argument(
        "--passive",
        type=int,
        default=15,
        help="seconds to listen before enabling RAW mode",
    )
    parser.add_argument(
        "--capture",
        type=Path,
        default=_ROOT / "testdata" / "accel-capture.jsonl",
        help="where to write the JSON-lines packet capture",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="do not wait for confirmation before connecting",
    )
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.yes and sys.stdin.isatty():
        input(
            "Pause the Home Assistant config entry (the ring serves one "
            "central), then press Enter to connect ... "
        )
    capture = Capture()
    try:
        await probe(args.address, args.passive, capture)
    except KeyboardInterrupt:
        print("\ninterrupted; reporting what was captured")
    report(capture)
    capture.write(args.capture)
    print(f"\nCapture written to {args.capture}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
