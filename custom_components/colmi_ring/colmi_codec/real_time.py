"""
Stream real time data from the ring.

Currently heart rate and SPO2 seem reasonable.

HRV, ECG, blood pressure and blood sugar seem unlikely to be something you
can correct
"""

from dataclasses import dataclass
from enum import IntEnum

from .packet import make_packet


class Action(IntEnum):
    START = 1
    PAUSE = 2
    CONTINUE = 3
    STOP = 4


class RealTimeReading(IntEnum):
    """
    Taken from https://colmi.puxtril.com/commands/#data-request
    """

    HEART_RATE = 1
    BLOOD_PRESSURE = 2
    SPO2 = 3
    FATIGUE = 4
    HEALTH_CHECK = 5
    # leaving this out as it's redundant
    # REAL_TIME_HEART_RATE = 6
    ECG = 7
    PRESSURE = 8
    BLOOD_SUGAR = 9
    HRV = 10


REAL_TIME_MAPPING: dict[str, RealTimeReading] = {
    "heart-rate": RealTimeReading.HEART_RATE,
    "blood-pressure": RealTimeReading.BLOOD_PRESSURE,
    "spo2": RealTimeReading.SPO2,
    "fatigue": RealTimeReading.FATIGUE,
    "health-check": RealTimeReading.HEALTH_CHECK,
    "ecg": RealTimeReading.ECG,
    "pressure": RealTimeReading.PRESSURE,
    "blood-sugar": RealTimeReading.BLOOD_SUGAR,
    "hrv": RealTimeReading.HRV,
}

CMD_START_REAL_TIME = 105
CMD_STOP_REAL_TIME = 106

CMD_REAL_TIME_HEART_RATE = 30
CONTINUE_HEART_RATE_PACKET = make_packet(CMD_REAL_TIME_HEART_RATE, bytearray(b"3"))


@dataclass
class Reading:
    kind: RealTimeReading
    value: int


@dataclass
class ReadingError:
    kind: RealTimeReading
    code: int


#: Start-frame sub-byte the vendor phone apps send for the long-lived
#: measurements. R08-Access-Bridge mirrors what the Colmi/QRing app
#: transmits on the wire: heart rate starts with a plain action byte, but
#: SpO2, stress and HRV start with 0x25.
VENDOR_START_SUBTYPE = 0x25

#: Readings whose start frame uses ``VENDOR_START_SUBTYPE`` instead of
#: ``Action.START``. Heart rate is deliberately absent: 0x01 is what every
#: supported ring (R02/R06/R08) has been measured to answer correctly.
_VENDOR_START_READINGS = frozenset(
    {RealTimeReading.SPO2, RealTimeReading.PRESSURE, RealTimeReading.HRV}
)


def get_start_packet(reading_type: RealTimeReading) -> bytearray:
    subtype = (
        VENDOR_START_SUBTYPE if reading_type in _VENDOR_START_READINGS else Action.START
    )
    return make_packet(CMD_START_REAL_TIME, bytearray([reading_type, subtype]))


def get_continue_packet(reading_type: RealTimeReading) -> bytearray:
    return make_packet(CMD_START_REAL_TIME, bytearray([reading_type, Action.CONTINUE]))


def get_stop_packet(reading_type: RealTimeReading) -> bytearray:
    return make_packet(CMD_STOP_REAL_TIME, bytearray([reading_type, 0, 0]))


def parse_real_time_reading(packet: bytearray) -> Reading | ReadingError:
    assert packet[0] == CMD_START_REAL_TIME

    kind = RealTimeReading(packet[1])
    error_code = packet[2]
    if error_code != 0:
        return ReadingError(kind=kind, code=error_code)

    return Reading(kind=kind, value=packet[3])


def parse_stop_reading(packet: bytearray) -> Reading | ReadingError | None:
    """
    Decode the reply to a ``get_stop_packet`` STOP frame (command 0x6A).

    Field-tested on an R08 by R08-Access-Bridge: this firmware returns the
    *final* measurement only in this reply, as ``[0x6A, kind, value, error]``
    - the value and error bytes are swapped relative to the 0x69 progress
    frames ``parse_real_time_reading`` decodes. Rings that instead answer
    STOP with zeros simply yield a ``Reading`` of 0, which callers treat as
    "no value", exactly like a zero progress frame.

    Returns ``None`` (packet ignored) if the kind byte names a reading we do
    not know, so a stray 0x6A frame cannot raise out of the dispatch path.
    """
    assert packet[0] == CMD_STOP_REAL_TIME

    try:
        kind = RealTimeReading(packet[1])
    except ValueError:
        return None

    value = packet[2]
    error_code = packet[3]
    if error_code != 0:
        return ReadingError(kind=kind, code=error_code)

    return Reading(kind=kind, value=value)
