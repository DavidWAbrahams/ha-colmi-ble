"""Payload parsers for YCBT frames: scalars and history record streams.

Record layouts follow the wire format reverse-engineered from HCI snoops
of the official companion app and the public YCBT SDK reference
(``com.yucheng.ycbtsdk.core.DataUnpack.unpackHealthData``). This
implementation is original to this integration.
All timestamps are returned as real Unix milliseconds via
:func:`.time_utils.read_ts2k_ms` (caller supplies the tz offset).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .time_utils import read_ts2k_ms

# Plausibility bounds (observed from companion-app behavior and the SDK).
HR_MIN, HR_MAX = 1, 239
BP_SYS_MAX, BP_DIA_MAX = 250, 200
SPO2_MIN, SPO2_MAX = 50, 100


# -------- Scalar parsers --------


def parse_battery_percent(payload: bytes) -> int | None:
    """Battery % from a ``GET_POWER_STATISTICS`` (0x0225) reply, byte 28."""
    if payload is None or len(payload) < 29:
        return None
    value = payload[28] & 0xFF
    return value if 0 <= value <= 100 else None


def parse_step_count(payload: bytes) -> int | None:
    """Day step count from a ``GET_NOW_STEP`` (0x020C) reply, u32 LE at 5."""
    if payload is None or len(payload) < 9:
        return None
    return (
        (payload[5] & 0xFF)
        | ((payload[6] & 0xFF) << 8)
        | ((payload[7] & 0xFF) << 16)
        | ((payload[8] & 0xFF) << 24)
    )


def parse_device_name(payload: bytes) -> str | None:
    """NUL-terminated device name from a ``GET_DEVICE_NAME`` (0x0203) reply."""
    if payload is None:
        return None
    end = len(payload)
    for i, byte in enumerate(payload):
        if byte == 0:
            end = i
            break
    return payload[:end].decode(errors="replace")


def parse_firmware_version(payload: bytes) -> str | None:
    """Firmware version ``"{maj}.{min}"`` from a device-info reply.

    Byte 2 = minor, byte 3 = major (little-endian field convention).
    """
    if payload is None or len(payload) < 4:
        return None
    return f"{payload[3] & 0xFF}.{payload[2] & 0xFF}"


def hr_from_snapshot(payload: bytes) -> int | None:
    """bpm from a ``REAL_UPLOAD_SNAPSHOT`` (0x0600) push, byte 4.

    The rest of that layout is still TBD; byte 4 is the only byte the
    companion app is known to consume (and it stays unverified there too).
    """
    if payload is None or len(payload) < 5:
        return None
    bpm = payload[4] & 0xFF
    return bpm if HR_MIN <= bpm <= HR_MAX else None


# -------- History record types --------


@dataclass(frozen=True)
class HrRecord:
    """One historical heart-rate reading (0x0515 stream, 6-byte records)."""

    timestamp_ms: int
    bpm: int


def parse_hr_records(payload: bytes, tz_offset_sec: int = 0) -> list[HrRecord]:
    """Parse a ``HEALTH_STREAM_HEART`` payload: ts(4 LE), rsv(1), bpm(1)."""
    out: list[HrRecord] = []
    if not payload:
        return out
    for i in range(0, len(payload) - 5, 6):
        bpm = payload[i + 5] & 0xFF
        if HR_MIN <= bpm <= HR_MAX:
            out.append(HrRecord(read_ts2k_ms(payload, i, tz_offset_sec), bpm))
    return out


@dataclass(frozen=True)
class BpRecord:
    """One historical blood-pressure reading (0x0517 stream, 8-byte records)."""

    timestamp_ms: int
    systolic: int
    diastolic: int
    hr: int


def parse_bp_records(payload: bytes, tz_offset_sec: int = 0) -> list[BpRecord]:
    """Parse a ``HEALTH_STREAM_BLOOD`` payload: ts(4), valid(1), sys, dia, hr."""
    out: list[BpRecord] = []
    if not payload:
        return out
    for i in range(0, len(payload) - 7, 8):
        sys_ = payload[i + 5] & 0xFF
        dia = payload[i + 6] & 0xFF
        hr = payload[i + 7] & 0xFF
        if 0 < sys_ <= BP_SYS_MAX and dia <= BP_DIA_MAX:
            out.append(
                BpRecord(read_ts2k_ms(payload, i, tz_offset_sec), sys_, dia, hr)
            )
    return out


@dataclass(frozen=True)
class SleepStage:
    """One stage inside a sleep session (8-byte record)."""

    TYPE_DEEP = 241
    TYPE_LIGHT = 242
    TYPE_REM = 243
    TYPE_AWAKE = 244

    type: int
    start_time_ms: int
    duration_sec: int


@dataclass
class SleepSession:
    """One decoded sleep session (per night) from the 0x0513 stream."""

    start_time_ms: int = 0
    end_time_ms: int = 0
    deep_sleep_count: int = 0
    light_sleep_count: int = 0
    deep_sleep_sec: int = 0
    light_sleep_sec: int = 0
    rem_sleep_sec: int = 0
    wake_count: int = 0
    wake_duration_sec: int = 0
    stages: list[SleepStage] = field(default_factory=list)


def parse_sleep_sessions(
    payload: bytes, tz_offset_sec: int = 0
) -> list[SleepSession]:
    """Parse a ``HEALTH_STREAM_SLEEP`` payload (20-byte header + N 8-byte stages)."""
    out: list[SleepSession] = []
    if not payload or len(payload) < 20:
        return out
    i = 0
    while i + 20 <= len(payload):
        session_len = (payload[i + 2] & 0xFF) | ((payload[i + 3] & 0xFF) << 8)
        start_ms = read_ts2k_ms(payload, i + 4, tz_offset_sec)
        end_ms = read_ts2k_ms(payload, i + 8, tz_offset_sec)
        sentinel = (payload[i + 12] & 0xFF) | ((payload[i + 13] & 0xFF) << 8)

        session = SleepSession(start_time_ms=start_ms, end_time_ms=end_ms)
        if sentinel == 0xFFFF:
            session.deep_sleep_count = (payload[i + 14] & 0xFF) | (
                ((payload[i + 15] & 0xFF) << 8)
            )
            session.light_sleep_count = 0
        else:
            session.deep_sleep_count = sentinel
            session.light_sleep_count = (payload[i + 14] & 0xFF) | (
                ((payload[i + 15] & 0xFF) << 8)
            )
        session.deep_sleep_sec = (
            (payload[i + 16] & 0xFF) | ((payload[i + 17] & 0xFF) << 8)
        ) * 60
        session.light_sleep_sec = (
            (payload[i + 18] & 0xFF) | ((payload[i + 19] & 0xFF) << 8)
        ) * 60

        stages_end = min(i + session_len, len(payload))
        j = i + 20
        while j + 8 <= stages_end:
            stage_type = payload[j] & 0xFF
            stage_ms = read_ts2k_ms(payload, j + 1, tz_offset_sec)
            dur = (
                (payload[j + 5] & 0xFF)
                | ((payload[j + 6] & 0xFF) << 8)
                | ((payload[j + 7] & 0xFF) << 16)
            )
            session.stages.append(SleepStage(stage_type, stage_ms, dur))
            if stage_type == SleepStage.TYPE_AWAKE:
                session.wake_count += 1
                session.wake_duration_sec += dur
            elif stage_type == SleepStage.TYPE_REM:
                session.rem_sleep_sec += dur
            j += 8

        out.append(session)
        # Advance to the next session header; a malformed (short) length
        # must still move forward so the walk can never spin forever.
        i += session_len if session_len >= 20 else 20
        if session_len < 20:
            break
    return out


@dataclass(frozen=True)
class AllRecord:
    """Composite 20-byte all-metrics record from the ``0x0518`` stream.

    Fields not consumed by this integration (rr/hrv/cvrr/temp/bodyfat/
    blood-sugar) are parsed anyway so the byte layout stays fully pinned.
    """

    timestamp_ms: int
    steps: int
    hr: int  # bpm, 0 = absent
    systolic: int  # mmHg, 0 = absent
    diastolic: int  # mmHg, 0 = absent
    spo2: int  # %, 0 = absent
    respiratory_rate: int
    hrv: int
    cvrr: int
    temperature: float  # degC, 0 = absent
    body_fat_pct: float  # %, 0 = absent
    blood_sugar: int  # mmol/L * 10, 0 = absent


def parse_all_records(payload: bytes, tz_offset_sec: int = 0) -> list[AllRecord]:
    """Parse a ``HEALTH_STREAM_ALL`` payload - 20-byte composite records."""
    out: list[AllRecord] = []
    if not payload:
        return out
    for i in range(0, len(payload) - 19, 20):
        ts = read_ts2k_ms(payload, i, tz_offset_sec)
        steps = (payload[i + 4] & 0xFF) | ((payload[i + 5] & 0xFF) << 8)
        hr = payload[i + 6] & 0xFF
        sys_ = payload[i + 7] & 0xFF
        dia = payload[i + 8] & 0xFF
        spo2 = payload[i + 9] & 0xFF
        rr = payload[i + 10] & 0xFF
        hrv = payload[i + 11] & 0xFF
        cvrr = payload[i + 12] & 0xFF
        t_int = payload[i + 13] & 0xFF
        t_frac = payload[i + 14] & 0xFF
        bf_int = payload[i + 15] & 0xFF
        bf_frac = payload[i + 16] & 0xFF
        bs = payload[i + 17] & 0xFF
        temperature = 0.0 if t_int == 0 and t_frac == 0 else t_int + t_frac / 100.0
        body_fat = 0.0 if bf_int == 0 and bf_frac == 0 else bf_int + bf_frac / 100.0
        out.append(
            AllRecord(
                ts,
                steps,
                hr,
                sys_,
                dia,
                spo2,
                rr,
                hrv,
                cvrr,
                temperature,
                body_fat,
                bs,
            )
        )
    return out


@dataclass(frozen=True)
class SportRecord:
    """14-byte sport-history record from the ``0x0511`` stream."""

    start_time_ms: int
    end_time_ms: int
    steps: int
    distance_meters: int
    calorie_kcal: int

    @property
    def timestamp_ms(self) -> int:
        """Alias so the generic history drain can watermark any record type."""
        return self.start_time_ms

    @property
    def duration_sec(self) -> int:
        return max(0, (self.end_time_ms - self.start_time_ms) // 1000)


def parse_sport_records(payload: bytes, tz_offset_sec: int = 0) -> list[SportRecord]:
    """Parse ``HEALTH_STREAM_SPORT``: start(4), end(4), steps u16, dist u16, kcal u16."""
    out: list[SportRecord] = []
    if not payload:
        return out
    for i in range(0, len(payload) - 13, 14):
        start = read_ts2k_ms(payload, i, tz_offset_sec)
        end = read_ts2k_ms(payload, i + 4, tz_offset_sec)
        steps = (payload[i + 8] & 0xFF) | ((payload[i + 9] & 0xFF) << 8)
        dist = (payload[i + 10] & 0xFF) | ((payload[i + 11] & 0xFF) << 8)
        kcal = (payload[i + 12] & 0xFF) | ((payload[i + 13] & 0xFF) << 8)
        out.append(SportRecord(start, end, steps, dist, kcal))
    return out


@dataclass(frozen=True)
class SportModeRecord:
    """25-byte manual-workout session record (opcode 0x052D, not live-verified)."""

    start_time_ms: int
    end_time_ms: int
    sport_steps: int
    distance_meters: int
    calorie_kcal: int
    sport_mode: int
    start_method: int  # 0 = manual from ring, 1 = firmware auto-detect
    avg_hr: int
    active_duration_sec: int
    min_hr: int
    max_hr: int

    @property
    def timestamp_ms(self) -> int:
        """Alias so the generic history drain can watermark any record type."""
        return self.start_time_ms


def parse_sport_mode_records(
    payload: bytes, tz_offset_sec: int = 0
) -> list[SportModeRecord]:
    """Parse manual-workout sessions streamed back under ``0x052D``."""
    out: list[SportModeRecord] = []
    if not payload:
        return out
    for i in range(0, len(payload) - 24, 25):
        start = read_ts2k_ms(payload, i, tz_offset_sec)
        end = read_ts2k_ms(payload, i + 4, tz_offset_sec)
        steps = (
            (payload[i + 8] & 0xFF)
            | ((payload[i + 9] & 0xFF) << 8)
            | ((payload[i + 10] & 0xFF) << 16)
            | ((payload[i + 11] & 0xFF) << 24)
        )
        dist = (payload[i + 12] & 0xFF) | ((payload[i + 13] & 0xFF) << 8)
        kcal = (payload[i + 14] & 0xFF) | ((payload[i + 15] & 0xFF) << 8)
        active = (
            (payload[i + 19] & 0xFF)
            | ((payload[i + 20] & 0xFF) << 8)
            | ((payload[i + 21] & 0xFF) << 16)
            | ((payload[i + 22] & 0xFF) << 24)
        )
        out.append(
            SportModeRecord(
                start,
                end,
                steps,
                dist,
                kcal,
                payload[i + 16] & 0xFF,
                payload[i + 17] & 0xFF,
                payload[i + 18] & 0xFF,
                active,
                payload[i + 23] & 0xFF,
                payload[i + 24] & 0xFF,
            )
        )
    return out
