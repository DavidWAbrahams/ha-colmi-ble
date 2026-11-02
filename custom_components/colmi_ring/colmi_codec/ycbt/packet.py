"""Build and parse YCBT wire frames (R20 protocol family).

The wire format and its magic prefixes were discovered via HCI snoop of
the official companion app; deviating from them makes the firmware
silently reject commands. This implementation is original to this
integration.

::

    [group(1)] [key(1)] [len_lo(1)] [len_hi(1)] [payload...] [crc_lo(1)] [crc_hi(1)]

with ``len = payload + 6``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import constants as yc


@dataclass(frozen=True)
class YcbtPacket:
    """One decoded frame: its opcode (group<<8|key) and payload bytes."""

    data_type: int
    payload: bytes

    def encode(self) -> bytearray:
        """Serialise back to the wire, recomputing the CRC."""
        total = len(self.payload) + 6
        out = bytearray(total)
        out[0] = (self.data_type >> 8) & 0xFF
        out[1] = self.data_type & 0xFF
        out[2] = total & 0xFF
        out[3] = (total >> 8) & 0xFF
        out[4 : 4 + len(self.payload)] = self.payload
        crc = crc16(out[: total - 2])
        out[total - 2] = crc & 0xFF
        out[total - 1] = (crc >> 8) & 0xFF
        return out


def crc16(data: bytes) -> int:
    """Yucheng CRC-16 variant. Seed 0xFFFF, byte-swap + nibble xor + poly bits."""
    state = 0xFFFF
    for byte in data:
        a = ((state << 8) & 0xFF00) | ((state >> 8) & 0xFF)
        b = (a ^ byte) & 0xFFFF
        c = (b ^ ((b & 0xFF) >> 4)) & 0xFFFF
        d = (c ^ ((c << 12) & 0xFFFF)) & 0xFFFF
        state = (d ^ (((d & 0xFF) << 5) & 0xFFFF)) & 0xFFFF
    return state & 0xFFFF


def decode(data: bytes) -> YcbtPacket | None:
    """Parse an inbound frame from a notification; ``None`` if malformed.

    Deliberately does *not* verify the CRC: some firmwares push frames whose
    CRC we have not reproduced, and the Java reference decodes unverified too.
    Callers that care can compare ``crc16(data[:-2])`` themselves.
    """
    if data is None or len(data) < 6:
        return None
    data_type = ((data[0] & 0xFF) << 8) | (data[1] & 0xFF)
    total = (data[2] & 0xFF) | ((data[3] & 0xFF) << 8)
    if total < 6 or total > len(data):
        return None
    return YcbtPacket(data_type, bytes(data[4 : total - 2]))


def crc_ok(data: bytes) -> bool:
    """True when the trailing CRC-16 (LE) matches the frame body."""
    if data is None or len(data) < 6:
        return False
    crc = crc16(data[:-2])
    return (data[-2] & 0xFF) == (crc & 0xFF) and (data[-1] & 0xFF) == (
        (crc >> 8) & 0xFF
    )


def is_rejection(packet: YcbtPacket) -> bool:
    """True for the firmware's single-byte ``0xFC`` 'not bound' refusal."""
    return len(packet.payload) == 1 and packet.payload[0] == yc.REJECTION_BYTE


# -------- Command builders --------


def setting_time(now: datetime) -> YcbtPacket:
    """Set the ring clock. ``now`` must be a wall-clock LOCAL time.

    Payload: ``[year_lo, year_hi, month, day, h, m, s, dow]`` with
    day-of-week Mon=0..Sun=6 (matches ``TimeUtil.makeBleTime`` in the
    YCBT SDK). The firmware stores this as wall-clock-local-as-UTC, which
    is why record timestamps need the tz fix in :mod:`.time_utils`.
    """
    year = now.year
    # Python weekday(): Mon=0..Sun=6 — already the firmware's convention.
    payload = bytes(
        [
            year & 0xFF,
            (year >> 8) & 0xFF,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.weekday(),
        ]
    )
    return YcbtPacket(yc.SETTING_TIME, payload)


def set_monitor_interval(minutes: int) -> YcbtPacket:
    """Background health-monitoring interval, 1..240 minutes."""
    minutes = max(1, min(240, int(minutes)))
    return YcbtPacket(yc.SET_MONITOR_INTERVAL, bytes([minutes]))


def enable_health_sensors(on: bool = True) -> YcbtPacket:
    """Enable the PPG / health-sensor hardware.

    Must precede any measurement: without it the firmware silently accepts
    start commands but never fires the PPG LEDs.
    """
    return YcbtPacket(yc.ENABLE_HEALTH_SENSORS, bytes([1 if on else 0]))


def enable_bg_spo2_monitor(on: bool = True) -> YcbtPacket:
    """Toggle background SpO2 monitoring."""
    return YcbtPacket(yc.ENABLE_BG_SPO2_MONITOR, bytes([1 if on else 0]))


def get_device_info() -> YcbtPacket:
    return YcbtPacket(yc.GET_DEVICE_INFO, yc.MAGIC_GET_DEVICE_INFO)


def get_device_name() -> YcbtPacket:
    return YcbtPacket(yc.GET_DEVICE_NAME, yc.MAGIC_GET_DEVICE_NAME)


def get_device_support_fn() -> YcbtPacket:
    return YcbtPacket(yc.GET_DEVICE_SUPPORT_FN, yc.MAGIC_GET_DEVICE_SUPPORT_FN)


def get_power_statistics() -> YcbtPacket:
    return YcbtPacket(yc.GET_POWER_STATISTICS, b"")


def get_now_step() -> YcbtPacket:
    return YcbtPacket(yc.GET_NOW_STEP, b"")


def get_real_blood_oxygen() -> YcbtPacket:
    return YcbtPacket(yc.GET_REAL_BLOOD_OXYGEN, yc.MAGIC_GET_REAL_BLOOD_OXYGEN)


def start_measurement(type_name: str, start: bool = True) -> YcbtPacket:
    """Start/stop a manual measurement session (PPG warmup ~10-20 s).

    ``type_name`` is a key of :data:`.constants.MEASURE_TYPES`. Payload is
    exactly ``[action, type]`` - verified by HCI snoop of the companion
    app and by the codec self-test.
    """
    mtype = yc.MEASURE_TYPES[type_name]
    return YcbtPacket(yc.APP_START_MEASUREMENT, bytes([1 if start else 0, mtype]))


def prime_sensors() -> YcbtPacket:
    """Pre-measurement sensor-mode toggle (0x030C ``[0x01, 0x01]``) observed
    in the companion app immediately before a start command."""
    return YcbtPacket(yc.APP_PRIME_SENSORS, bytes([0x01, 0x01]))


def health_history(request_opcode: int) -> YcbtPacket:
    """Empty-payload history fetch (e.g. ``HEALTH_HISTORY_HEART``)."""
    return YcbtPacket(request_opcode, b"")


def history_ack(stream_data_type: int, block_len: int) -> YcbtPacket:
    """ACK one received history block; firmware streams the next chunk.

    Payload: ``[stream_hi, stream_lo, block_len_lo, block_len_hi]``.
    """
    return YcbtPacket(
        yc.HEALTH_HISTORY_ACK,
        bytes(
            [
                (stream_data_type >> 8) & 0xFF,
                stream_data_type & 0xFF,
                block_len & 0xFF,
                (block_len >> 8) & 0xFF,
            ]
        ),
    )
