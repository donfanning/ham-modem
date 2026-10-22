"""AX.25 v2.0/2.2 frame codec: addresses, control field, PID, and TNC-2 style monitor text.

Address fields are six shifted ASCII characters plus an SSID octet (``CRRSSID0``; the last address sets bit 0).
In destination/source the C bits mark command/response; in digipeater addresses the same bit is H, "has been
repeated". Only modulo-8 control fields are decoded here; modulo-128 (SABME) links are not supported yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PID_NO_L3 = 0xF0
# Unnumbered control values with the P/F bit cleared.
SABM, SABME, DISC, DM, UA, FRMR, UI, XID, TEST = 0x2F, 0x6F, 0x43, 0x0F, 0x63, 0x87, 0x03, 0xAF, 0xE3
RR, RNR, REJ, SREJ = 0x01, 0x05, 0x09, 0x0D
PF = 0x10
_U_NAMES = {SABM: "SABM", SABME: "SABME", DISC: "DISC", DM: "DM", UA: "UA", FRMR: "FRMR", UI: "UI", XID: "XID",
            TEST: "TEST"}
_S_NAMES = {RR: "RR", RNR: "RNR", REJ: "REJ", SREJ: "SREJ"}
_CALL_RE = re.compile(r"^([A-Z0-9]{1,6})(?:-(\d{1,2}))?(\*)?$")


class Ax25Error(ValueError):
    pass


@dataclass
class Address:
    call: str
    ssid: int = 0
    flag: bool = False                     # C bit (dest/source) or H bit (digipeaters)

    @classmethod
    def parse(cls, text: str) -> "Address":
        """``N0CALL``, ``N0CALL-7`` or ``WIDE1-1*`` (the star marks a digipeater that has repeated the frame)."""
        m = _CALL_RE.match(text.strip().upper())
        if not m or int(m.group(2) or 0) > 15:
            raise Ax25Error(f"bad callsign {text!r}")
        return cls(m.group(1), int(m.group(2) or 0), bool(m.group(3)))

    def encode(self, last: bool) -> bytes:
        call = self.call.upper().ljust(6)
        return (bytes((ord(c) << 1) & 0xFE for c in call)
                + bytes((0x60 | (0x80 if self.flag else 0) | (self.ssid << 1) | (1 if last else 0),)))

    @classmethod
    def decode(cls, raw: bytes) -> "Address":
        call = "".join(chr(b >> 1) for b in raw[:6]).rstrip()
        if not call or any(not (c.isalnum() and c.isascii()) for c in call):
            raise Ax25Error(f"bad address {raw!r}")
        return cls(call, (raw[6] >> 1) & 0x0F, bool(raw[6] & 0x80))

    def __str__(self) -> str:
        return self.call + (f"-{self.ssid}" if self.ssid else "")


@dataclass
class Frame:
    dest: Address
    src: Address
    digis: list[Address] = field(default_factory=list)
    control: int = UI
    pid: int | None = PID_NO_L3
    info: bytes = b""
    command: bool = True                   # AX.25 v2: command sets dest C, response sets source C

    @classmethod
    def ui(cls, src: str, dest: str, info: bytes | str, path: str | list[str] = "") -> "Frame":
        if isinstance(path, str):
            path = [p for p in path.split(",") if p]
        if isinstance(info, str):
            info = info.encode("latin-1")
        return cls(Address.parse(dest), Address.parse(src), [Address.parse(p) for p in path], UI, PID_NO_L3, info)

    def encode(self) -> bytes:
        if len(self.digis) > 8:
            raise Ax25Error("at most 8 digipeaters")
        dest = Address(self.dest.call, self.dest.ssid, self.command)
        src = Address(self.src.call, self.src.ssid, not self.command)
        out = dest.encode(False) + src.encode(not self.digis)
        for i, d in enumerate(self.digis):
            out += d.encode(i == len(self.digis) - 1)
        out += bytes((self.control,))
        if self.has_pid:
            out += bytes((self.pid if self.pid is not None else PID_NO_L3,))
        return out + self.info

    @classmethod
    def decode(cls, raw: bytes) -> "Frame":
        n = 0
        addrs: list[Address] = []
        while True:
            if n + 7 > len(raw) or len(addrs) > 9:
                raise Ax25Error("truncated address field")
            addrs.append(Address.decode(raw[n:n + 7]))
            n += 7
            if raw[n - 1] & 1:
                break
        if len(addrs) < 2 or n >= len(raw):
            raise Ax25Error("frame too short")
        control = raw[n]
        n += 1
        frame = cls(addrs[0], addrs[1], addrs[2:], control, None, b"")
        # v2 command/response: dest C=1/src C=0 is a command; v1 frames (both equal) count as commands.
        frame.command = addrs[0].flag or not addrs[1].flag
        if frame.has_pid:
            if n >= len(raw):
                raise Ax25Error("missing PID")
            frame.pid = raw[n]
            n += 1
        frame.info = raw[n:]
        return frame

    @property
    def has_pid(self) -> bool:
        return (self.control & 1) == 0 or (self.control & ~PF) == UI

    @property
    def kind(self) -> str:
        c = self.control
        if c & 1 == 0:
            return "I"
        if c & 3 == 1:
            return _S_NAMES.get(c & 0x0F, "S?")
        return _U_NAMES.get(c & ~PF, "U?")

    @property
    def poll(self) -> bool:
        return bool(self.control & PF)

    @property
    def ns(self) -> int:
        return (self.control >> 1) & 7

    @property
    def nr(self) -> int:
        return (self.control >> 5) & 7

    def path_text(self) -> str:
        """TNC-2 monitor header: ``SRC>DEST,DIGI1*,DIGI2``."""
        text = f"{self.src}>{self.dest}"
        for d in self.digis:
            text += f",{d}" + ("*" if d.flag else "")
        return text

    def monitor(self) -> str:
        """One-line TNC-2 monitor rendering; UI frames show just the text, others show the frame type."""
        info = self.info.decode("latin-1").rstrip("\r\n")
        if self.kind == "UI":
            return f"{self.path_text()}:{info}"
        detail = self.kind
        if self.kind == "I":
            detail = f"I R{self.nr} S{self.ns}"
        elif self.kind in _S_NAMES.values():
            detail = f"{self.kind} R{self.nr}"
        flag = ("P" if self.command else "F") if self.poll else ""
        text = f"{self.path_text()} <{detail}{(' ' + flag) if flag else ''}>"
        return f"{text}:{info}" if self.info else text
