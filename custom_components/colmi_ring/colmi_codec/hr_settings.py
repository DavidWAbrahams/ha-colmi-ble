"""
Heart rate log settings for controlling if the ring should record heart rate periodically, and if so how often to record.

An odd packet set up as it's either a query for the current settings or trying to set the settings.

I don't know what byte 1 in the response is.
"""

import logging
from dataclasses import dataclass

from .packet import make_packet

CMD_HEART_RATE_LOG_SETTINGS = 22  # 0x16

READ_HEART_RATE_LOG_SETTINGS_PACKET = make_packet(
    CMD_HEART_RATE_LOG_SETTINGS, bytearray(b"\x01")
)

logger = logging.getLogger(__name__)


@dataclass
class HeartRateLogSettings:
    enabled: bool
    interval: int
    """Interval in minutes"""
    extra: bytes = b"\x00\x00\x00\x00"
    """
    Reply bytes 4..7, echoed back verbatim on every write.

    R08-class (QRing) firmware fills these with its own settings - per the
    R08-Access-Bridge's ``ControlProtocol`` they are ``startInterval``,
    ``tooLow``, ``tooHigh`` and ``mainSwitch`` - and its write frame
    re-sends the read-back values unchanged. Zeroing them (what a bare
    3-byte write does) overwrites fields the ring never told us, which a
    firmware that validates them can treat as turning logging off. R02
    firmware answers these bytes as 0, so echoing them is a no-op there.
    """


def parse_heart_rate_log_settings(packet: bytearray) -> HeartRateLogSettings:
    r"""
    example: bytearray(b'\x16\x01\x01<\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00T')

    R08-class firmware fills bytes 4..7 with its own extra settings (e.g.
    ``16 01 02 3C 05 00 00 00``: disabled, 60-min interval, startInterval 5);
    keep them raw so a later write can echo them back untouched.
    """
    assert packet[0] == CMD_HEART_RATE_LOG_SETTINGS

    raw_enabled = packet[2]
    if raw_enabled == 1:
        enabled = True
    elif raw_enabled == 2:
        enabled = False
    else:
        logger.warning(
            f"Unexpected value in enabled byte {raw_enabled}, defaulting to false"
        )
        enabled = False

    return HeartRateLogSettings(
        enabled=enabled, interval=packet[3], extra=bytes(packet[4:8])
    )


def hr_log_settings_packet(settings: HeartRateLogSettings) -> bytearray:
    assert 0 < settings.interval < 256, "Interval must be between 0 and 255"
    assert len(settings.extra) == 4, "Extra settings bytes must be the ring's 4 bytes"
    enabled = 1 if settings.enabled else 2
    sub_data = bytearray([2, enabled, settings.interval, *settings.extra])
    return make_packet(CMD_HEART_RATE_LOG_SETTINGS, sub_data)
