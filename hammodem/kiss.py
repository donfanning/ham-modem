"""KISS TNC framing (Chepponis & Karn, 1987) plus the TNC parameter commands.

Each frame is ``FEND type data FEND`` with FEND/FESC escaped in the data. The type octet's high nibble is the
port, the low nibble the command: 0 data, 1 TXDELAY (10 ms units), 2 P (persistence, 0-255), 3 SLOTTIME (10 ms),
4 TXTAIL (10 ms), 5 FULLDUPLEX, 6 SETHARDWARE; 0xFF (whole octet) means leave KISS mode.
"""

from __future__ import annotations

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
DATA, TXDELAY, PERSIST, SLOTTIME, TXTAIL, FULLDUPLEX, SETHARDWARE = range(7)
RETURN = 0xFF
MAX_KISS_FRAME = 4096


def escape(data: bytes) -> bytes:
    return data.replace(bytes((FESC,)), bytes((FESC, TFESC))).replace(bytes((FEND,)), bytes((FESC, TFEND)))


def encode(data: bytes, port: int = 0, command: int = DATA) -> bytes:
    return bytes((FEND, ((port & 0x0F) << 4) | (command & 0x0F))) + escape(data) + bytes((FEND,))


class Decoder:
    """Streaming KISS decoder: ``feed`` returns complete ``(port, command, data)`` tuples.

    A ``RETURN`` (0xFF) frame is reported as ``(15, RETURN, b"")``. Oversized frames are dropped.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._esc = False

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        out = []
        for b in data:
            if b == FEND:
                if self._in_frame and self._buf:
                    t = self._buf[0]
                    if t == RETURN:
                        out.append((15, RETURN, b""))
                    else:
                        out.append((t >> 4, t & 0x0F, bytes(self._buf[1:])))
                self._buf.clear()
                self._in_frame, self._esc = True, False
                continue
            if not self._in_frame:
                continue
            if self._esc:
                self._esc = False
                b = {TFEND: FEND, TFESC: FESC}.get(b, b)
            elif b == FESC:
                self._esc = True
                continue
            self._buf.append(b)
            if len(self._buf) > MAX_KISS_FRAME:
                self._buf.clear()
                self._in_frame = False
        return out
