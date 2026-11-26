#!/usr/bin/env python3
"""
_temperature_selftest.py

Throwaway validation for the Colmi skin-temperature codec (colmi_codec.big_data
+ colmi_codec.temperature): checks the CRC-16/MODBUS primitive, the ``bc 25``
request/reply framing against the R09 protocol doc's golden frames, the
notification reassembler (fragmentation, resync, bad-CRC drop), the 50-byte
block decode (raw -> degC and slot times), and the 0x3A all-day preference
packet/parse. Run from the repo root:

    python scripts/_temperature_selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "colmi_ring"))

from colmi_codec import big_data, temperature  # noqa: E402
from colmi_codec.date_utils import start_of_day  # noqa: E402


def check_crc16() -> None:
    # The standard CRC-16/MODBUS check value: CRC("123456789") == 0x4B37.
    assert big_data.crc16(b"123456789") == 0x4B37, "MODBUS check string"
    # The R09's real 0x25 request payload is the single byte 0x02.
    assert big_data.crc16(bytearray([0x02])) == 0x813E, "request payload CRC"
    # The R09's empty 0x25 reply payload is 00 1e.
    assert big_data.crc16(bytearray([0x00, 0x1E])) == 0xB881, "empty reply CRC"
    print("ok  crc16 MODBUS + R09 golden payloads")


def check_request_frame() -> None:
    # Golden request from the protocol doc: bc 25 01 00 3e 81 02.
    # build_request lays out lead/type/len-LE/crc-LE then the payload byte.
    frame = big_data.build_request(big_data.TYPE_TEMPERATURE, bytearray([0x02]))
    assert frame == bytearray([0xBC, 0x25, 0x01, 0x00, 0x3E, 0x81, 0x02]), (
        f"request frame mismatch: {frame.hex(' ')}"
    )
    # The codec's read packet for "2 days ago" must reproduce that exact frame.
    assert temperature.read_temperature_packet(2) == frame, (
        "read_temperature_packet(2) != golden request"
    )
    # days_ago selects the single payload byte.
    assert temperature.read_temperature_packet(0)[6] == 0x00
    print("ok  bc 25 request framing")


def _reassemble(chunks: list[bytes]) -> list[tuple[int, bytes]]:
    reassembler = big_data.BigDataReassembler()
    out: list[tuple[int, bytes]] = []
    for chunk in chunks:
        out.extend(reassembler.feed(bytearray(chunk)))
    return out


def check_reassembler_whole_frame() -> None:
    # Golden empty reply: bc 25 02 00 81 b8 00 1e -> (0x25, b"\\x00\\x1e").
    golden = bytearray([0xBC, 0x25, 0x02, 0x00, 0x81, 0xB8, 0x00, 0x1E])
    frames = _reassemble([bytes(golden)])
    assert frames == [(0x25, b"\x00\x1e")], f"whole frame: {frames}"
    print("ok  reassembler whole frame")


def check_reassembler_fragmented() -> None:
    # A long reply is fragmented across notifications; pieces must concatenate.
    block = bytearray([0x00, 0x1E] + [0x00] * 48)
    frame = big_data.build_request(big_data.TYPE_TEMPERATURE, block)
    assert len(frame) == big_data.HEADER_LEN + 50 == 56
    # Split at awkward boundaries (20-byte MTU-ish).
    chunks = [bytes(frame[0:20]), bytes(frame[20:40]), bytes(frame[40:])]
    frames = _reassemble(chunks)
    assert frames == [(0x25, bytes(block))], f"fragmented: {frames}"
    print("ok  reassembler fragments concatenate")


def check_reassembler_two_frames_one_feed() -> None:
    # One notification can complete one frame and begin another.
    a = big_data.build_request(big_data.TYPE_TEMPERATURE, bytearray([0x00]))
    b = big_data.build_request(big_data.TYPE_SLEEP, bytearray([0x07]))
    both = bytearray(a) + bytearray(b)
    frames = _reassemble([bytes(both)])
    assert frames == [(0x25, b"\x00"), (0x27, b"\x07")], f"two-in-one: {frames}"
    print("ok  reassembler two frames in one feed")


def check_reassembler_resync() -> None:
    # Junk before a good frame (no stray lead byte) is dropped, frame survives.
    good = big_data.build_request(big_data.TYPE_TEMPERATURE, bytearray([0x00]))
    frames = _reassemble([bytes(bytearray([0x00, 0x11])) + bytes(good)])
    assert frames == [(0x25, b"\x00")], f"resync: {frames}"
    print("ok  reassembler resyncs past junk")


def check_reassembler_bad_crc_dropped() -> None:
    # A corrupt payload CRC is dropped; a following good frame still parses.
    good = big_data.build_request(big_data.TYPE_TEMPERATURE, bytearray([0x00]))
    bad = bytearray(good)
    bad[-1] ^= 0xFF  # break the payload so the CRC no longer matches
    frames = _reassemble([bytes(bad) + bytes(good)])
    assert frames == [(0x25, b"\x00")], f"bad crc dropped: {frames}"
    print("ok  reassembler drops bad CRC, keeps following frame")


def _one_block(days_ago: int = 0, *, raw_at_29: int = 0xA9) -> bytearray:
    block = bytearray([days_ago, 0x1E] + [0x00] * 48)
    block[2 + 29] = raw_at_29
    return block


def check_block_decode() -> None:
    payload = _one_block(days_ago=0)
    logs = temperature.parse_temperature_blocks(payload)
    assert len(logs) == 1, f"one block -> one log, got {len(logs)}"
    log = logs[0]
    assert log.slot_minutes == 30, f"step byte 0x1e -> 30, got {log.slot_minutes}"
    assert log.values[29] == 0xA9, "raw kept as-is at slot 29"

    samples = log.samples_with_times()
    assert len(samples) == 1, f"only the one non-zero slot, got {len(samples)}"
    degc, ts = samples[0]
    # raw 0xa9 (169) -> 169/10 + 20 = 36.9 C, slot 29 -> 14:30.
    assert abs(degc - 36.9) < 1e-6, f"decode 36.9 C, got {degc}"
    assert ts.hour == 14 and ts.minute == 30, f"slot 29 -> 14:30, got {ts.time()}"
    assert ts.date() == start_of_day(_now()).date(), "today block anchors to today"
    print("ok  block decode raw->degC + slot time")


def check_empty_block_payload() -> None:
    # The ring's "nothing recorded" answer (00 1e) is shorter than a block.
    assert temperature.parse_temperature_blocks(bytearray([0x00, 0x1E])) == []
    print("ok  short payload -> no logs")


def _now():
    from colmi_codec import date_utils

    return date_utils.now()


def check_pref_packets() -> None:
    read = temperature.read_temp_pref_packet()
    assert len(read) == 16, "pref read packet is 16 bytes"
    assert read[0] == 0x3A and read[1] == 0x03 and read[2] == 0x01, (
        f"read 3a 03 01, got {read[0:3].hex(' ')}"
    )
    assert read[15] == (read[0] + read[1] + read[2]) & 0xFF, "read crc"

    on = temperature.write_temp_pref_packet(True)
    assert on[0] == 0x3A and on[1] == 0x03 and on[2] == 0x02 and on[3] == 0x01, (
        f"write-on 3a 03 02 01, got {on[0:4].hex(' ')}"
    )
    off = temperature.write_temp_pref_packet(False)
    assert off[3] == 0x00, f"write-off enable byte 0, got {off[3]}"
    print("ok  0x3A preference packet layout")


def check_pref_parse() -> None:
    # Doc example: after turning it on the ring answers 3a 03 01 01 ...
    assert (
        temperature.parse_temp_pref_reply(bytearray([0x3A, 0x03, 0x01, 0x01, 0, 0]))
        is True
    )
    assert (
        temperature.parse_temp_pref_reply(bytearray([0x3A, 0x03, 0x01, 0x00, 0, 0]))
        is False
    )
    print("ok  0x3A preference parse (byte 3)")


def main() -> int:
    check_crc16()
    check_request_frame()
    check_reassembler_whole_frame()
    check_reassembler_fragmented()
    check_reassembler_two_frames_one_feed()
    check_reassembler_resync()
    check_reassembler_bad_crc_dropped()
    check_block_decode()
    check_empty_block_payload()
    check_pref_packets()
    check_pref_parse()
    print("all temperature codec checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
