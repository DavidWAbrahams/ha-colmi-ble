"""
Colmi "big-data" channel framing (the second vendor service, ``de5bf728``).

The same service that carries the 16-byte command protocol on newer Colmi
firmware (R09/R10/R12) also carries a second, *bulk* protocol for the longer
per-day histories - skin temperature (``0x25``), sleep (``0x27``), SpO2
(``0x2a``) - which the fixed 14-byte-payload command frame cannot fit. A
big-data frame looks nothing like a command packet:

    ``bc <type> <len LE u16> <crc16 LE u16> <payload>``

with ``len`` the payload length, and ``crc16`` a CRC-16/MODBUS (poly 0xA001,
init 0xFFFF) over the payload only, transmitted little-endian. The frame's
total length is therefore ``6 + len``.

Two things make this channel need its own handling in the transport:

- a whole frame is far longer than one BLE notification, so the ring
  fragments it across successive notifications (20 bytes at the default 23-byte
  MTU) and the pieces must be concatenated back together, and
- ``0xbc`` frames arrive on the same notify characteristic as the 16-byte
  command replies, so they have to be recognised (by the ``0xbc`` lead) and
  diverted from the command dispatch.

:class:`BigDataReassembler` does the concatenation and CRC check; it is fed
each inbound notification as-is and emits one ``(type, payload)`` tuple per
complete, CRC-valid frame. Verified on a real R09 (protocol doc, 2026-11): the
standard CRC check string ``"123456789"`` yields 0x4B37, the 0x25 request
``bc 25 01 00 3e 81 02`` and the empty 0x25 reply ``bc 25 02 00 81 b8 00 1e``
both carry a correct MODBUS CRC over their payloads.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Frame lead byte distinguishing a big-data frame from a command packet.
LEAD = 0xBC

# Bytes before the payload: lead, type, 2-byte length, 2-byte CRC.
HEADER_LEN = 6
# Type bytes of the per-day histories carried on this channel.
TYPE_TEMPERATURE = 0x25
TYPE_SLEEP = 0x27
TYPE_SPO2 = 0x2A


def crc16(data: bytearray | bytes | list[int]) -> int:
    """
    CRC-16/MODBUS of ``data`` (poly 0xA001, init 0xFFFF).

    This is the standard ``"123456789"`` check equals 0x4B37 variant; the ring
    transmits the result little-endian. Confirmed against the R09's real 0x25
    request and reply frames.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_request(type_id: int, payload: bytes | bytearray) -> bytearray:
    """
    Assemble a big-data request frame for ``type_id`` with ``payload``.

    The frame's length and CRC fields are filled from the payload, so the
    caller only supplies the command-specific bytes (for temperature that is
    the single "days ago" byte).
    """
    length = len(payload)
    frame = bytearray(HEADER_LEN + length)
    frame[0] = LEAD
    frame[1] = type_id
    frame[2] = length & 0xFF
    frame[3] = (length >> 8) & 0xFF
    crc = crc16(payload)
    frame[4] = crc & 0xFF
    frame[5] = (crc >> 8) & 0xFF
    frame[HEADER_LEN:] = payload
    return frame


class BigDataReassembler:
    """
    Reassemble CRC-validated big-data frames from notification fragments.

    Feed every inbound notification (whatever its length) to :meth:`feed`; it
    appends to an internal buffer and returns a list of ``(type, payload)``
    tuples for each *complete, CRC-valid* frame now in the buffer. Fragments,
    a buffer still mid-frame, and frames with a bad CRC (which are dropped so
    a corrupt burst cannot wedge the buffer) all yield no output.

    The reassembler is deliberately stateless about which metric it is
    collecting: it emits every well-formed frame it can see, and the caller
    keeps only the type it asked for. A big-data response is always requested
    from a fresh transaction, so a stray frame left in the buffer from a
    previous exchange cannot answer a later request; :meth:`reset` clears it
    between transactions regardless.
    """

    def __init__(self) -> None:
        """Start with an empty buffer."""
        self._buf = bytearray()

    def reset(self) -> None:
        """Discard any partial frame so the next burst parses cleanly."""
        self._buf.clear()

    def feed(self, chunk: bytearray | bytes) -> list[tuple[int, bytes]]:
        """Append one notification; return each frame completed by it."""
        self._buf.extend(chunk)
        frames: list[tuple[int, bytes]] = []
        # A notification can complete one frame and already carry the start of
        # the next, so keep draining while a whole frame sits in the buffer.
        while True:
            if len(self._buf) < HEADER_LEN:
                break
            if self._buf[0] != LEAD:
                # Resynchronise: a mid-stream drop or an unexpected byte. The
                # frame we thought we were collecting is unusable, so drop
                # until the next lead (or empty).
                next_lead = self._buf.find(LEAD, 1)
                if next_lead < 0:
                    self._buf.clear()
                    break
                del self._buf[:next_lead]
                continue
            length = self._buf[2] | (self._buf[3] << 8)
            total = HEADER_LEN + length
            if len(self._buf) < total:
                # Frame header looks sane but its tail has not arrived yet.
                break
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            payload = frame[HEADER_LEN:]
            expected = frame[4] | (frame[5] << 8)
            actual = crc16(payload)
            if expected != actual:
                logger.debug(
                    "Discarding big-data frame type 0x%02X with bad CRC"
                    " (expected 0x%04X, computed 0x%04X)",
                    frame[1],
                    expected,
                    actual,
                )
                continue
            frames.append((frame[1], payload))
        return frames
