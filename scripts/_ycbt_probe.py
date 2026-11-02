#!/usr/bin/env python3
"""Phase 0 discovery probe for Yucheng YCBT-SDK rings (R20 family / R99).

Dev-only tool; not part of the integration. It talks to the ring with plain
``bleak`` from this workstation, speaking the frame format implemented in
``colmi_codec/ycbt``, and answers the questions that gate Phase 1 for a
ring this integration has never seen:

  1. Does the ring expose the vendor service ``be940000-...`` with the
     ``be940001`` write / ``be940003`` notify characteristics, or does it
     alias them under ``0xFEE7`` with different UUIDs (as the R99's
     advertisement hints)? The full GATT map is printed either way.
  2. Does the init burst (set time -> enable sensors -> Get* info commands)
     get accepted, or answered with the ``0xFC`` "not bound" rejection?
  3. Do the history drains (``0x0506`` HR, ``0x0509`` all-metrics) stream,
     and do record timestamps land near "now"?
  4. Do the real-time pushes (``0x0601`` HR / ``0x0602`` SpO2, or the
     standard ``0x2A37`` HRS characteristic) fire during a manual
     measurement?

The ring serves a single central. Pause or disable the Home Assistant config
entry (and keep the ring close to this machine's Bluetooth adapter) before
running, then::

    python scripts/_ycbt_probe.py --yes --drain

A JSON-lines capture of every frame (with opcode + CRC verdicts) is written
for offline replay and to become golden test fixtures::

    python scripts/_ycbt_probe.py --drain --measure hr

CRC note: the firmware's CRC-16 is reproduced in the codec, but decode never
depends on it; the probe reports how many frames fail ``crc_ok`` so a
device-specific CRC variant would show up immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bleak import BleakClient

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "custom_components" / "colmi_ring"))

from colmi_codec.ycbt import constants as yc  # noqa: E402
from colmi_codec.ycbt import packet as yc_pkt  # noqa: E402
from colmi_codec.ycbt import records as yc_rec  # noqa: E402

DEFAULT_ADDRESS = "07:35:00:04:6B:81"

UUID_HRS_SERVICE_SHORT = "0000180d-0000-1000-8000-00805f9b34fb"
UUID_HRS_CHAR_SHORT = "00002a37-0000-1000-8000-00805f9b34fb"

#: Human-readable opcode names for the summary (introspected from the codec).
OPCODE_NAMES: dict[int, str] = {
    value: name
    for name, value in vars(yc).items()
    if name.isupper() and isinstance(value, int) and 0 < value <= 0xFFFF
}


def opcode_name(opcode: int) -> str:
    return OPCODE_NAMES.get(opcode, "?")


def tz_offset_sec() -> int:
    """Host-local UTC offset, for converting ts2k timestamps."""
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds()) if offset else 0


# ---------------------------------------------------------------- capture


@dataclass
class Frame:
    """One notification frame, timestamped on the monotonic clock."""

    t: float
    char: str
    data: bytes
    opcode: int | None
    crc_valid: bool


@dataclass
class Capture:
    """Every frame seen, plus named time markers for each probe phase."""

    frames: list[Frame] = field(default_factory=list)
    marks: list[tuple[float, str]] = field(default_factory=list)
    rejections: int = 0
    hrs_beats: int = 0

    def note(self, label: str) -> None:
        self.marks.append((time.monotonic(), label))

    def add(self, char: str, data: bytes) -> Frame:
        raw = bytes(data)
        packet = yc_pkt.decode(raw)
        frame = Frame(
            t=time.monotonic(),
            char=char,
            data=raw,
            opcode=packet.data_type if packet is not None else None,
            crc_valid=yc_pkt.crc_ok(raw),
        )
        self.frames.append(frame)
        return frame

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for mark_t, label in sorted(self.marks):
                handle.write(json.dumps({"t": mark_t, "mark": label}) + "\n")
            for frame in self.frames:
                handle.write(
                    json.dumps(
                        {
                            "t": frame.t,
                            "char": frame.char,
                            "hex": frame.data.hex(),
                            "opcode": frame.opcode,
                            "crc_ok": frame.crc_valid,
                        }
                    )
                    + "\n"
                )


# ---------------------------------------------------------------- transport


def _writable_no_resp(char) -> bool:
    return "write-without-response" in char.properties


def _notifiable(char) -> bool:
    return "notify" in char.properties or "indicate" in char.properties


class Link:
    """The write characteristic, its notification characteristics, and HRS."""

    def __init__(self, client: BleakClient) -> None:
        self.write_char: Any = None
        self.notify_chars: list[Any] = []
        self.hrs_char: Any = None

        vendor = [
            s
            for s in client.services
            if s.uuid.lower()
            in (yc.UUID_SERVICE.lower(), "0000fee7-0000-1000-8000-00805f9b34fb")
        ]
        for service in vendor:
            for char in service.characteristics:
                if self.write_char is None and _writable_no_resp(char):
                    self.write_char = char
                if _notifiable(char):
                    self.notify_chars.append(char)
            # Prefer the documented be940001 when present.
            for char in service.characteristics:
                if yc.UUID_CHAR_WRITE in char.uuid.lower():
                    self.write_char = char
                    break
        for service in client.services:
            if service.uuid.lower() == UUID_HRS_SERVICE_SHORT:
                for char in service.characteristics:
                    if char.uuid.lower() == UUID_HRS_CHAR_SHORT:
                        self.hrs_char = char

    def describe(self, client: BleakClient) -> None:
        print("\nGATT map:")
        for service in client.services:
            print(f"  service {service.uuid}")
            for char in service.characteristics:
                print(f"    char {char.uuid} [{','.join(char.properties)}]")
        print(f"\n  write  : {self.write_char.uuid if self.write_char else 'NONE'}")
        for char in self.notify_chars:
            print(f"  listen : {char.uuid}")
        print(f"  hrs    : {self.hrs_char.uuid if self.hrs_char else 'NONE'}")


async def send(client: BleakClient, char: Any, packet: yc_pkt.YcbtPacket) -> None:
    wire = packet.encode()
    print(f"  >> 0x{packet.data_type:04X} {bytes(wire).hex(' ')}")
    await client.write_gatt_char(char, wire, response=False)


# ---------------------------------------------------------------- listeners


def hrs_bpm(data: bytes) -> int | None:
    """Decode a standard 0x2A37 value, honouring the sensor-contact bit."""
    if not data:
        return None
    flags = data[0]
    if (flags & 0x06) == 0x04:  # contact supported, not worn -> ignore
        return None
    if flags & 0x01:
        return int.from_bytes(data[1:3], "little") if len(data) >= 3 else None
    return data[1] if len(data) >= 2 else None


class FrameBus:
    """Fans decoded frames out to per-opcode async waiters (for the drain)."""

    def __init__(self, capture: Capture, hrs_uuid: str = "") -> None:
        self.capture = capture
        self.hrs_uuid = hrs_uuid.lower()
        self.waiters: list[tuple[set[int], asyncio.Future]] = []
        self.last_hr: int | None = None
        self.last_spo2: int | None = None

    def make_callback(self):
        def on_notify(char, data: bytearray) -> None:
            if self.hrs_uuid and char.uuid.lower() == self.hrs_uuid:
                # Standard HRS value: not a YCBT frame; decode its own way.
                self.capture.hrs_beats += 1
                bpm = hrs_bpm(bytes(data))
                if bpm:
                    self.last_hr = bpm
                    print(f"  << 0x2A37 live hr: {bpm} bpm")
                return
            frame = self.capture.add(char.uuid, bytes(data))
            self._dispatch(frame)

        return on_notify

    def _dispatch(self, frame: Frame) -> None:
        if frame.opcode is None:
            return
        packet = yc_pkt.decode(frame.data)
        if packet is None:
            return
        if yc_pkt.is_rejection(packet):
            self.capture.rejections += 1
            print("  << !! 0xFC firmware rejection (not bound)")
            return
        if frame.opcode == yc.REAL_UPLOAD_HEART and len(packet.payload) >= 1:
            self.last_hr = packet.payload[0] & 0xFF
        elif frame.opcode == yc.REAL_UPLOAD_BLOOD_OXY and len(packet.payload) >= 1:
            self.last_spo2 = packet.payload[0] & 0xFF
        for opcodes, future in list(self.waiters):
            if frame.opcode in opcodes and not future.done():
                future.set_result(frame)

    async def wait(self, opcodes: set[int], timeout: float) -> Frame | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        entry = (opcodes, future)
        self.waiters.append(entry)
        try:
            return await asyncio.wait_for(future, timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        finally:
            self.waiters.remove(entry)


# ---------------------------------------------------------------- phases


async def wait(capture: Capture, seconds: float, why: str = "") -> None:
    if why:
        print(f"  .. listening {seconds:.0f}s ({why})")
    await asyncio.sleep(seconds)


async def init_burst(
    client: BleakClient, link: Link, bus: FrameBus, capture: Capture
) -> bool:
    """Send the companion-app init sequence; re-send once on 0xFC rejection.

    Returns True if the burst was accepted (no rejection seen on the retry).
    """
    assert link.write_char is not None
    for attempt in (1, 2):
        capture.note(f"init_burst_{attempt}")
        rejections_before = capture.rejections
        await send(client, link.write_char, yc_pkt.setting_time(datetime.now()))
        await asyncio.sleep(0.2)
        await send(client, link.write_char, yc_pkt.set_monitor_interval(5))
        await asyncio.sleep(0.2)
        await send(client, link.write_char, yc_pkt.enable_bg_spo2_monitor(True))
        await asyncio.sleep(0.2)
        await send(client, link.write_char, yc_pkt.enable_health_sensors(True))
        await asyncio.sleep(0.2)
        for cmd in (
            yc_pkt.get_device_name(),
            yc_pkt.get_device_info(),
            yc_pkt.get_power_statistics(),
            yc_pkt.get_now_step(),
        ):
            await send(client, link.write_char, cmd)
            await asyncio.sleep(0.2)
        await wait(capture, 4.0, "init burst replies")
        if capture.rejections == rejections_before:
            return True
        print("  !! ring answered 0xFC; retrying burst once (stale bond recovery)")
    return False


async def drain(
    client: BleakClient,
    link: Link,
    bus: FrameBus,
    capture: Capture,
    request_opcode: int,
    parse: Callable[[bytes, int], list],
    seconds: int,
) -> int:
    """Drain one history stream with HWM-conditional ACKs.

    Mirrors the sync policy of the reference implementation: a block is ACKed
    (advancing the firmware's stream) only when it carries records newer than
    our in-memory high-water mark; once a block brings nothing new we stop
    ACKing, which parks the stream without deleting anything from the ring.
    """
    stream = yc.STREAM_FOR_REQUEST[request_opcode]
    tz = tz_offset_sec()
    hwm = 0
    total_new = 0
    assert link.write_char is not None

    capture.note(f"drain_0x{request_opcode:04X}")
    await send(client, link.write_char, yc_pkt.health_history(request_opcode))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        frame = await bus.wait({stream}, timeout=max(0.5, deadline - loop.time()))
        if frame is None:
            break
        packet = yc_pkt.decode(frame.data)
        if packet is None:
            continue
        rows = parse(packet.payload, tz)
        newest = max((r.timestamp_ms for r in rows), default=0)
        fresh = [r for r in rows if r.timestamp_ms > hwm]
        print(
            f"  << stream 0x{stream:04X} block {len(packet.payload)}B:"
            f" {len(rows)} records ({len(fresh)} new)"
        )
        for row in fresh[:5]:
            stamp = datetime.fromtimestamp(
                row.timestamp_ms / 1000, tz=timezone.utc
            ).astimezone()
            print(f"       {stamp:%Y-%m-%d %H:%M} {row}")
        if not fresh:
            print("  .. block carried nothing new; stopping ACK (stream parks)")
            break
        total_new += len(fresh)
        hwm = max(hwm, newest)
        await send(
            client,
            link.write_char,
            yc_pkt.history_ack(stream, len(packet.payload) + 6),
        )
    return total_new


async def measure(
    client: BleakClient,
    link: Link,
    bus: FrameBus,
    capture: Capture,
    kind: str,
    seconds: int,
) -> None:
    """Trigger a manual measurement and watch for the real-time pushes."""
    assert link.write_char is not None
    capture.note(f"measure_{kind}")
    await send(client, link.write_char, yc_pkt.prime_sensors())
    await asyncio.sleep(0.3)
    await send(client, link.write_char, yc_pkt.start_measurement(kind, start=True))
    print(f"  .. measuring {kind}; wear the ring, keep still")
    deadline = time.monotonic() + seconds
    last_shown = None
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        value = bus.last_hr if kind == "heart_rate" else bus.last_spo2
        if value and value != last_shown:
            last_shown = value
            print(f"  << live {kind}: {value}")
    await send(client, link.write_char, yc_pkt.start_measurement(kind, start=False))
    await asyncio.sleep(0.5)


# ---------------------------------------------------------------- report


def report(capture: Capture) -> None:
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    counts: Counter[int | None] = Counter(f.opcode for f in capture.frames)
    total = len(capture.frames)
    crc_bad = sum(
        1 for f in capture.frames if f.opcode is not None and not f.crc_valid
    )
    print(f"frames: {total}  undecodable: {counts[None]}  crc-mismatched: {crc_bad}")
    print(f"0xFC rejections: {capture.rejections}  0x2A37 HRS samples: {capture.hrs_beats}")
    for opcode, count in sorted((k, v) for k, v in counts.items() if k is not None):
        print(f"  0x{opcode:04X} {opcode_name(opcode):<28} x{count}")

    payloads: dict[int, bytes] = {}
    for frame in capture.frames:
        if frame.opcode is None:
            continue
        packet = yc_pkt.decode(frame.data)
        if packet is not None:
            payloads.setdefault(frame.opcode, packet.payload)
    if yc.GET_DEVICE_NAME in payloads:
        print(f"name     : {yc_rec.parse_device_name(payloads[yc.GET_DEVICE_NAME])}")
    if yc.GET_DEVICE_INFO in payloads:
        print(
            f"firmware : {yc_rec.parse_firmware_version(payloads[yc.GET_DEVICE_INFO])}"
        )
    if yc.GET_POWER_STATISTICS in payloads:
        print(
            "battery  :"
            f" {yc_rec.parse_battery_percent(payloads[yc.GET_POWER_STATISTICS])}%"
        )
    if yc.GET_NOW_STEP in payloads:
        print(f"steps    : {yc_rec.parse_step_count(payloads[yc.GET_NOW_STEP])}")


# ---------------------------------------------------------------- main


async def probe(args: argparse.Namespace, capture: Capture) -> None:
    print(f"Connecting to {args.address} ...")
    async with BleakClient(args.address, timeout=30.0) as client:
        link = Link(client)
        link.describe(client)
        if link.write_char is None:
            raise SystemExit(
                "no write-without-response characteristic on be940000/FEE7;"
                " is HA holding the ring?"
            )

        bus = FrameBus(capture, hrs_uuid=link.hrs_char.uuid if link.hrs_char else "")
        on_notify = bus.make_callback()
        subscribed: list[Any] = []
        for char in link.notify_chars + ([link.hrs_char] if link.hrs_char else []):
            try:
                await client.start_notify(char, on_notify)
                subscribed.append(char)
            except Exception as err:  # noqa: BLE001
                print(f"  !! cannot subscribe {char.uuid}: {err}")

        try:
            capture.note("passive")
            await wait(capture, args.passive, "passive listen, sending nothing")

            accepted = await init_burst(client, link, bus, capture)
            if not accepted:
                print(
                    "  !! ring still rejects commands after retry; continuing"
                    " anyway (the capture is still informative)"
                )

            if args.drain:
                await drain(
                    client,
                    link,
                    bus,
                    capture,
                    yc.HEALTH_HISTORY_HEART,
                    yc_rec.parse_hr_records,
                    args.drain_timeout,
                )
                await drain(
                    client,
                    link,
                    bus,
                    capture,
                    yc.HEALTH_HISTORY_ALL,
                    yc_rec.parse_all_records,
                    args.drain_timeout,
                )

            if args.measure:
                await measure(
                    client, link, bus, capture, args.measure, args.measure_seconds
                )
        finally:
            for char in subscribed:
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
        default=8,
        help="seconds of passive listening before the init burst",
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        help="drain the HR (0x0506) and All (0x0509) history streams",
    )
    parser.add_argument(
        "--drain-timeout", type=int, default=25, help="max seconds per stream"
    )
    parser.add_argument(
        "--measure",
        choices=["hr", "spo2"],
        default=None,
        help="trigger a manual measurement and watch the pushes",
    )
    parser.add_argument("--measure-seconds", type=int, default=30)
    parser.add_argument(
        "--capture",
        type=Path,
        default=_ROOT / "testdata" / "ycbt-capture.jsonl",
        help="where to write the JSON-lines frame capture",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="do not wait for confirmation before connecting",
    )
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.measure == "hr":
        args.measure = "heart_rate"
    elif args.measure == "spo2":
        args.measure = "blood_oxygen"
    if not args.yes and sys.stdin.isatty():
        input(
            "Pause the Home Assistant config entry (the ring serves one "
            "central), then press Enter to connect ... "
        )
    capture = Capture()
    try:
        await probe(args, capture)
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
