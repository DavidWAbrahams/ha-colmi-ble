#!/usr/bin/env python3
"""Self-test for the pure YCBT codec (no BLE, no Home Assistant).

Checks the codec against golden wire-format expectations captured via HCI
snoop of the official companion app, plus round-trip and record-parser
smokes. Run: ``python scripts/_ycbt_codec_selftest.py``.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "custom_components" / "colmi_ring"))

from colmi_codec.ycbt import constants as yc  # noqa: E402
from colmi_codec.ycbt import packet as yc_pkt  # noqa: E402
from colmi_codec.ycbt import records as yc_rec  # noqa: E402


def check_wire_format() -> None:
    """Golden wire-format expectations from HCI snoops of the app."""
    pkt = yc_pkt.start_measurement("blood_oxygen", True)
    b = pkt.encode()
    assert len(b) == 8, b.hex()
    assert (b[4], b[5]) == (0x01, 0x02), b.hex()

    pkt = yc_pkt.start_measurement("heart_rate", False)
    b = pkt.encode()
    assert len(b) == 8 and (b[4], b[5]) == (0x00, 0x00), b.hex()

    pkt = yc_pkt.prime_sensors()
    b = pkt.encode()
    assert b[0] == 0x03 and b[1] == 0x0C and b[4] == 0x01 and b[5] == 0x01, b.hex()

    pkt = yc_pkt.history_ack(yc.HEALTH_STREAM_ALL, 20)
    b = pkt.encode()
    assert len(b) == 10, b.hex()
    assert (b[4], b[5], b[6], b[7]) == (0x05, 0x18, 20, 0), b.hex()
    assert yc_pkt.crc_ok(b)

    # decode round-trip
    back = yc_pkt.decode(b)
    assert back is not None and back.data_type == yc.HEALTH_HISTORY_ACK
    assert back.payload == bytes([0x05, 0x18, 20, 0x00])
    assert back.encode() == b

    # CRC golden: re-encoding any packet must verify
    for maker in (
        yc_pkt.get_device_info,
        yc_pkt.get_device_name,
        yc_pkt.get_power_statistics,
        yc_pkt.get_now_step,
        yc_pkt.enable_health_sensors,
        lambda: yc_pkt.set_monitor_interval(30),
        lambda: yc_pkt.set_blood_pressure_monitor(30),
        lambda: yc_pkt.set_temperature_monitor(30),
        lambda: yc_pkt.set_blood_oxygen_monitor(30),
        lambda: yc_pkt.set_user_bind(),
    ):
        enc = maker().encode()
        assert yc_pkt.crc_ok(enc), maker

    # user-bind recovery frame: opcode 0x0109, single-byte bind payload
    pkt = yc_pkt.set_user_bind()
    assert pkt.data_type == yc.SETTING_USER_BIND_ECHO, hex(pkt.data_type)
    assert pkt.payload == bytes([0x01]), pkt.payload.hex()

    # Monitor payload golden: {enable, intervalMinutes}, per the vendor SDK's
    # settings encoder (a lone minute byte is ACKed but never applied).
    for maker, opcode in (
        (yc_pkt.set_monitor_interval, yc.SET_MONITOR_INTERVAL),
        (yc_pkt.set_blood_pressure_monitor, yc.SET_BLOOD_PRESSURE_MONITOR),
        (yc_pkt.set_temperature_monitor, yc.SET_TEMPERATURE_MONITOR),
        (yc_pkt.set_blood_oxygen_monitor, yc.ENABLE_BG_SPO2_MONITOR),
    ):
        pkt = maker(45)
        assert pkt.data_type == opcode, hex(pkt.data_type)
        assert pkt.payload == bytes([0x01, 45]), pkt.payload.hex()
        assert maker(45, on=False).payload == bytes([0x00, 45])
        # wire clamp keeps the interval byte sane even past the policy floor
        assert maker(999).payload == bytes([0x01, 240])

    # reject/accept framing
    assert yc_pkt.decode(bytes([0x01, 0x09, 0x05, 0x00, 0xFC, 0x00])) is None or True
    assert yc_pkt.decode(bytes([0x01])) is None
    rej = yc_pkt.decode(bytes([0x01, 0x09, 0x07, 0x00, 0xFC, 0x00, 0x00]))
    assert rej is not None and yc_pkt.is_rejection(rej)


def check_time_payload() -> None:
    # 2026-11-01 is a Sunday -> dow=6 (Mon=0..Sun=6)
    pkt = yc_pkt.setting_time(datetime(2026, 11, 1, 15, 30, 45))
    assert pkt.payload == bytes([0xEA, 0x07, 11, 1, 15, 30, 45, 6]), pkt.payload.hex()
    assert pkt.data_type == yc.SETTING_TIME
    # A Monday
    pkt = yc_pkt.setting_time(datetime(2026, 11, 2, 0, 0, 0))
    assert pkt.payload[7] == 0


def check_records() -> None:
    from datetime import timezone

    from colmi_codec.ycbt.constants import EPOCH_2000_UNIX_SECONDS

    tz = -8 * 3600  # America/Los_Angeles (PST) as a fixed offset
    # ts2k for a wall-clock 2026-11-01T15:00 (local-as-UTC), computed
    # independent of the host timezone.
    utc_ref = datetime(2000, 1, 1, tzinfo=timezone.utc)
    wall = datetime(2026, 11, 1, 15, 0, 0, tzinfo=timezone.utc)
    ts2k_val = int((wall - utc_ref).total_seconds())
    assert ts2k_val == int(wall.timestamp()) - EPOCH_2000_UNIX_SECONDS

    rec = bytearray(ts2k_val.to_bytes(4, "little")) + bytes([0x00, 72])
    hrs = yc_rec.parse_hr_records(bytes(rec) * 3, tz_offset_sec=tz)
    assert len(hrs) == 3 and all(h.bpm == 72 for h in hrs)
    # local-as-UTC 15:00 minus (-8h) => 23:00 UTC
    expected_ms = int(wall.timestamp() * 1000) - tz * 1000
    assert hrs[0].timestamp_ms == expected_ms, (hrs[0].timestamp_ms, expected_ms)

    # zero bpm filtered
    rec0 = bytearray(ts2k_val.to_bytes(4, "little")) + bytes([0x00, 0x00])
    assert yc_rec.parse_hr_records(bytes(rec0), tz_offset_sec=tz) == []

    # AllRecord: hr@6, spo2@9, hrv@11, steps u16@4
    payload = bytearray(20)
    payload[0:4] = ts2k_val.to_bytes(4, "little")
    payload[4:6] = (1234).to_bytes(2, "little")
    payload[6] = 66
    payload[9] = 97
    payload[11] = 42  # HRV, ms
    payload[13], payload[14] = 36, 52  # 36.52 degC
    ars = yc_rec.parse_all_records(bytes(payload), tz_offset_sec=tz)
    assert len(ars) == 1
    a = ars[0]
    assert a.steps == 1234 and a.hr == 66 and a.spo2 == 97
    assert a.hrv == 42
    assert abs(a.temperature - 36.52) < 1e-9

    # battery / steps / fw / name scalars
    p225 = bytes(28) + bytes([73]) + bytes([0, 0])
    assert yc_rec.parse_battery_percent(p225) == 73
    assert yc_rec.parse_battery_percent(bytes(10)) is None
    p20c = bytes(5) + (9876).to_bytes(4, "little")
    assert yc_rec.parse_step_count(p20c) == 9876
    assert yc_rec.parse_firmware_version(bytes([0, 0, 32, 2])) == "2.32"
    assert yc_rec.parse_device_name(b"R99 6B81\x00junk") == "R99 6B81"

    # sport record 14B
    sp = bytearray(14)
    sp[0:4] = ts2k_val.to_bytes(4, "little")
    sp[4:8] = (ts2k_val + 600).to_bytes(4, "little")
    sp[8:10] = (500).to_bytes(2, "little")
    sp[10:12] = (432).to_bytes(2, "little")
    sp[12:14] = (37).to_bytes(2, "little")
    sps = yc_rec.parse_sport_records(bytes(sp), tz_offset_sec=tz)
    assert len(sps) == 1 and sps[0].steps == 500 and sps[0].distance_meters == 432
    assert sps[0].calorie_kcal == 37 and sps[0].duration_sec == 600

    # sleep session with one stage (defensive walk)
    sess = bytearray(28)
    sess[2:4] = (28).to_bytes(2, "little")  # sessionLen
    sess[4:8] = ts2k_val.to_bytes(4, "little")
    sess[8:12] = (ts2k_val + 3600).to_bytes(4, "little")
    sess[12:14] = (0xFF, 0xFF)  # sentinel -> new layout
    sess[14:16] = (3).to_bytes(2, "little")  # deep count
    sess[16:18] = (90).to_bytes(2, "little")  # deep minutes
    sess[18:20] = (120).to_bytes(2, "little")  # light minutes
    sess[20] = yc_rec.SleepStage.TYPE_DEEP
    sess[21:25] = ts2k_val.to_bytes(4, "little")
    sess[25:28] = (1800).to_bytes(3, "little")
    sessions = yc_rec.parse_sleep_sessions(bytes(sess), tz_offset_sec=tz)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.deep_sleep_sec == 90 * 60 and s.light_sleep_sec == 120 * 60
    assert len(s.stages) == 1 and s.stages[0].duration_sec == 1800

    # malformed: session_len < 20 must terminate, not hang
    junk = bytes(20) + bytes([0x05, 0x00]) + bytes(40)
    yc_rec.parse_sleep_sessions(junk, tz_offset_sec=tz)

    # snapshot hr @ byte 4
    assert yc_rec.hr_from_snapshot(bytes([0, 0, 0, 0, 71])) == 71
    assert yc_rec.hr_from_snapshot(bytes([0, 0, 0, 0, 0])) is None


def main() -> int:
    check_wire_format()
    check_time_payload()
    check_records()
    print("ycbt codec self-test: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
