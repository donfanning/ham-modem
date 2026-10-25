"""SIP audio leg: one call carrying the modem's audio, driven from the TNC's select() loop.

Lab-scale SIP over UDP, after El-Modem's ``sip_uac.py``/``sip_uas.py``/``sip_auth.py`` (the message parser and
the digest code are copied from there), restructured to be non-blocking:

* ``dial`` mode places a call to ``ext@host[:port]`` (a ConfBridge room, a peer station) and keeps it up --
  re-dialling after a hangup or failure, since for a TNC the call *is* the radio channel.
* ``listen`` mode answers one incoming call at a time (point-to-point links: another station dials us).

Handles 100/180/183, 401/407 digest challenges, INVITE retransmission, in-dialog BYE, re-INVITE and OPTIONS.
Not handled: TCP/TLS, SRTP, Record-Route/Route sets, forking.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import socket
import time
import uuid
import wave
from pathlib import Path

import numpy as np

from .rtp import CODECS, Codec, RtpSession, frame_ms

log = logging.getLogger("hammodem.sip")

T1 = 0.5


def parse_message(data: bytes) -> tuple[str, dict[str, str], str]:
    text = data.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers: dict[str, str] = {}
    compact = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
               "c": "content-type"}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            k = key.strip().lower()
            k = compact.get(k, k)
            headers[k] = headers[k] + "," + value.strip() if k in headers else value.strip()
    return lines[0] if lines else "", headers, body


def parse_challenge(header: str) -> dict[str, str]:
    body = header.split(None, 1)[1] if " " in header else ""
    return {k.lower(): (q if q else v) for k, q, v in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', body)}


def digest_response(user: str, password: str, method: str, uri: str, ch: dict[str, str], nc: int = 1) -> str:
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()          # noqa: E731 -- MD5 is what SIP digest specifies
    realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
    ha1, ha2 = md5(f"{user}:{realm}:{password}"), md5(f"{method}:{uri}")
    qop = "auth" if "auth" in [q.strip() for q in ch.get("qop", "").split(",")] else ""
    if qop:
        cnonce = uuid.uuid4().hex[:16]
        response = md5(f"{ha1}:{nonce}:{nc:08x}:{cnonce}:{qop}:{ha2}")
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")
    out = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}", '
           "algorithm=MD5")
    if qop:
        out += f', qop={qop}, nc={nc:08x}, cnonce="{cnonce}"'
    if "opaque" in ch:
        out += f', opaque="{ch["opaque"]}"'
    return out


def build_sdp(ip: str, port: int, codecs: list[Codec], session: str = "ham-modem") -> str:
    ptime = frame_ms(codecs[0].rate) if codecs else 20
    pts = " ".join(str(c.pt) for c in codecs)
    lines = ["v=0", f"o=- {int(time.time())} 1 IN IP4 {ip}", f"s={session}", f"c=IN IP4 {ip}", "t=0 0",
             f"m=audio {port} RTP/AVP {pts}"]
    lines += [f"a=rtpmap:{c.pt} {c.name}/{c.rate}" for c in codecs]
    lines += [f"a=ptime:{ptime}", "a=sendrecv"]
    return "\r\n".join(lines) + "\r\n"


def parse_sdp(body: str) -> tuple[tuple[str, int] | None, list[tuple[int, str, int]]]:
    """Remote media address and its payload types as (pt, encoding, rate), in preference order."""
    ip = port = None
    pts: list[int] = []
    maps: dict[int, tuple[str, int]] = {0: ("PCMU", 8000), 8: ("PCMA", 8000)}
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        elif line.startswith("m=audio "):
            parts = line.split()
            port = int(parts[1]) if parts[1].isdigit() else None
            pts = [int(p) for p in parts[3:] if p.isdigit()]
        elif line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+) ([\w.-]+)/(\d+)", line)
            if m:
                maps[int(m.group(1))] = (m.group(2).upper(), int(m.group(3)))
    offered = [(pt, *maps[pt]) for pt in pts if pt in maps]
    return ((ip, port) if ip and port else None), offered


def match_codec(offered: list[tuple[int, str, int]], ours: list[Codec]) -> Codec | None:
    """First remote payload type we support; a dynamic PT is adopted as the peer numbered it."""
    for pt, name, rate in offered:
        for c in ours:
            if c.name == name and c.rate == rate:
                return Codec(c.name, pt, c.rate)
    return None


def _uri_in(header: str) -> str | None:
    m = re.search(r"<([^>]+)>", header)
    return m.group(1) if m else (header.split(";")[0].strip() or None)


def local_ip_towards(host: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class SipLeg:
    """The audio-leg interface (``fds/service/send/frame_samples/name/close``) over a SIP call."""

    def __init__(self, *, dial: str | None = None, listen: bool = False, bind_host: str = "0.0.0.0",
                 sip_port: int = 0, media_port: int = 0, codecs: list[Codec] | None = None,
                 user: str | None = None, password: str | None = None, from_user: str = "ham-modem",
                 local_ip: str | None = None, redial_after: float = 5.0, sim_loss: float = 0.0,
                 sim_tx_silence_every: int = 0, sim_tx_drop_every: int = 0,
                 sim_tx_gain_db: float = 0.0, sim_mask_tone_hz: float = 0.0,
                 sim_mask_gain_db: float = -30.0, sim_mask_wav: str | None = None) -> None:
        """Lab impairments: receive-side RTP drops and periodic silent transmit payloads.

        ``sim_tx_silence_every`` replaces each Nth non-silent outgoing audio block with silence while still sending
        its RTP packet. ``sim_tx_drop_every`` omits each Nth active packet after advancing its RTP sequence and
        timestamp. The latter models station-to-bridge packet loss.
        """
        if not dial and not listen:
            raise ValueError("SipLeg needs dial= or listen=True")
        self.codecs = codecs or [CODECS["pcmu"], CODECS["pcma"]]
        self.rate = self.codecs[0].rate
        if any(c.rate != self.rate for c in self.codecs):
            raise ValueError("all offered codecs must share the TNC's sample rate")
        self.frame_samples = self.rate * frame_ms(self.rate) // 1000
        self.user, self._password, self.from_user = user, password, user or from_user
        self.redial_after = redial_after
        self.sim_loss = sim_loss
        if sim_tx_silence_every < 0 or sim_tx_drop_every < 0:
            raise ValueError("transmit impairment intervals must be >= 0")
        if sim_tx_silence_every and sim_tx_drop_every:
            raise ValueError("choose either transmit silence or transmit drop impairment")
        self.sim_tx_silence_every = sim_tx_silence_every
        self.sim_tx_drop_every = sim_tx_drop_every
        self.sim_tx_gain = 10.0 ** (sim_tx_gain_db / 20.0)
        if sim_mask_tone_hz < 0 or sim_mask_tone_hz >= self.rate / 2:
            raise ValueError("sim_mask_tone_hz must be between 0 and Nyquist")
        self.sim_mask_tone_hz = sim_mask_tone_hz
        self.sim_mask_amplitude = 32767.0 * 10.0 ** (sim_mask_gain_db / 20.0)
        self.sim_mask_gain = 10.0 ** (sim_mask_gain_db / 20.0)
        if sim_mask_wav and sim_mask_tone_hz:
            raise ValueError("choose either a mask tone or a mask WAV")
        self._mask_audio = None
        self._mask_audio_offset = 0
        if sim_mask_wav:
            with wave.open(str(Path(sim_mask_wav)), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != \
                        (1, 2, self.rate, "NONE"):
                    raise ValueError(f"mask WAV must be mono, 16-bit PCM at {self.rate} Hz")
                self._mask_audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").copy()
            if not len(self._mask_audio):
                raise ValueError("mask WAV must not be empty")
        self._mask_phase = 0
        self.tx_masker_blocks = 0
        self._tx_active_blocks = 0
        self.tx_silenced_blocks = 0
        self.tx_dropped_blocks = 0
        self._rng = random.Random()
        self.target: tuple[str, int] | None = None
        self.target_uri = ""
        if dial:
            ext, _, hostport = dial.rpartition("@")
            host, _, port = hostport.partition(":")
            self.target = (socket.gethostbyname(host), int(port or 5060))
            self.target_uri = f"sip:{ext}@{hostport}"
        self.local_ip = local_ip or local_ip_towards(self.target[0] if self.target else "8.8.8.8")
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind((bind_host, sip_port))
        self.sip.setblocking(False)
        self.media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.media.bind((bind_host, media_port))
        self.media.setblocking(False)
        self.sip_port, self.media_port = self.sip.getsockname()[1], self.media.getsockname()[1]
        self.name = self.target_uri if dial else f"sip listen {self.local_ip}:{self.sip_port}"
        self.state = "idle"                  # idle -> calling -> up -> idle (dial) / idle -> up (listen)
        self.rtp: RtpSession | None = None
        self._d: dict = {}
        self._next_action = time.monotonic()  # dial now
        self._retx_at = 0.0
        self._retx_interval = T1
        self._last_invite = b""
        self.calls = 0
        self.on_state = None                  # callable(state, detail) for logs / DCD-like status

    # ---------------------------------------------------------------- leg interface

    def fds(self):
        return [self.sip, self.media]

    def service(self, readable) -> list[np.ndarray]:
        if self.sip in readable:
            while True:
                try:
                    data, addr = self.sip.recvfrom(65535)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    continue
                self._on_sip(data, addr)
        blocks: list[np.ndarray] = []
        if self.media in readable:
            while True:
                try:
                    data, addr = self.media.recvfrom(4096)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    continue
                if self.rtp is not None and self.state == "up":
                    if self.sim_loss and self._rng.random() < self.sim_loss:
                        continue
                    blocks += self.rtp.receive(data, addr)
        self._timers()
        return blocks

    def send(self, pcm: np.ndarray) -> None:
        if self.rtp is not None and self.state == "up":
            pcm = np.asarray(pcm)
            mask_audio = getattr(self, "_mask_audio", None)
            if (self.sim_mask_tone_hz or mask_audio is not None) and not np.any(pcm):
                if mask_audio is not None:
                    offsets = (np.arange(len(pcm)) + self._mask_audio_offset) % len(mask_audio)
                    pcm = np.clip(np.rint(mask_audio[offsets].astype(np.float64) * self.sim_mask_gain),
                                  -32768, 32767).astype(np.int16)
                    self._mask_audio_offset = (self._mask_audio_offset + len(pcm)) % len(mask_audio)
                else:
                    idx = np.arange(len(pcm)) + self._mask_phase
                    phase = 2.0 * np.pi * self.sim_mask_tone_hz * idx / self.rate
                    pcm = np.rint(self.sim_mask_amplitude * np.sin(phase)).astype(np.int16)
                    self._mask_phase += len(pcm)
                self.tx_masker_blocks += 1
            if self.sim_tx_gain != 1.0:
                pcm = np.clip(np.rint(pcm.astype(np.float64) * self.sim_tx_gain), -32768, 32767).astype(np.int16)
            drop = False
            if (self.sim_tx_silence_every or self.sim_tx_drop_every) and np.any(pcm):
                self._tx_active_blocks += 1
                if self.sim_tx_silence_every and self._tx_active_blocks % self.sim_tx_silence_every == 0:
                    pcm = np.zeros_like(pcm)
                    self.tx_silenced_blocks += 1
                elif self.sim_tx_drop_every and self._tx_active_blocks % self.sim_tx_drop_every == 0:
                    drop = True
                    self.tx_dropped_blocks += 1
            self.rtp.send(pcm, drop=drop)

    def close(self) -> None:
        if self.state == "up":
            self._send_bye()
        elif self.state == "calling":
            self._send_cancel()
        self.sip.close()
        self.media.close()

    # ---------------------------------------------------------------- outgoing call

    def _timers(self) -> None:
        now = time.monotonic()
        if self.target and self.state == "idle" and now >= self._next_action:
            self._invite()
        elif self.state == "calling" and self._last_invite and now >= self._retx_at:
            if now - self._d["started"] > 32 * T1:
                self._set_state("idle", "no answer")
                self._next_action = now + self.redial_after
                return
            self._send(self._last_invite, self.target)
            self._retx_interval = min(self._retx_interval * 2, 4.0)
            self._retx_at = now + self._retx_interval

    def _invite(self, auth: str | None = None) -> None:
        d = self._d
        if auth is None:
            d.clear()
            d.update(call_id=f"{uuid.uuid4()}@{self.local_ip}", cseq=1, started=time.monotonic(),
                     local=f"<sip:{self.from_user}@{self.local_ip}>;tag={uuid.uuid4().hex[:10]}",
                     remote=f"<{self.target_uri}>", ruri=self.target_uri, peer=self.target)
        else:
            d["cseq"] += 1
        d["branch"] = "z9hG4bK" + uuid.uuid4().hex
        sdp = build_sdp(self.local_ip, self.media_port, self.codecs)
        msg = self._request("INVITE", d["ruri"], [f"Contact: <sip:{self.from_user}@{self.local_ip}:{self.sip_port}>",
                                                  "Content-Type: application/sdp"] + ([auth] if auth else []), sdp,
                            branch=d["branch"])
        self._last_invite = msg
        self._send(msg, self.target)
        self._retx_interval = T1
        self._retx_at = time.monotonic() + T1
        if auth is None:
            self._set_state("calling", self.target_uri)

    def _on_response(self, start: str, h: dict[str, str], body: str) -> None:
        d = self._d
        parts = start.split(" ", 2)
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        method = h.get("cseq", "").split()[-1:] or [""]
        if h.get("call-id") != d.get("call_id") or method[0] != "INVITE":
            return
        if 100 <= code < 200:
            self._retx_at = float("inf")      # provisional: stop retransmitting (Timer A)
            return
        if self.state != "calling":
            if code == 200:                   # retransmitted 200: our ACK was lost
                self._ack(h, success=True)
            return
        if code in (401, 407) and self.user and not d.get("challenged"):
            d["challenged"] = True
            self._ack(h)
            ch = parse_challenge(h.get("www-authenticate") or h.get("proxy-authenticate", ""))
            name = "Authorization" if code == 401 else "Proxy-Authorization"
            self._invite(f"{name}: " + digest_response(self.user, self._password or "", "INVITE", d["ruri"], ch))
            return
        if 200 <= code < 300:
            d["remote"] = h.get("to", d["remote"])
            d["ruri"] = _uri_in(h.get("contact", "")) or d["ruri"]
            self._ack(h, success=True)
            remote, offered = parse_sdp(body)
            codec = match_codec(offered, self.codecs)
            if remote is None or codec is None:
                log.warning("answer has no usable audio (%s); hanging up", offered)
                self.state = "up"
                self._send_bye()
                self._set_state("idle", "no common codec")
                self._next_action = time.monotonic() + self.redial_after
                return
            self._start_media(codec, remote)
            return
        self._ack(h)
        self._set_state("idle", f"SIP {code} {parts[2] if len(parts) > 2 else ''}".strip())
        self._next_action = time.monotonic() + self.redial_after

    def _ack(self, h: dict[str, str], success: bool = False) -> None:
        """ACK a final response: a 2xx ACK is its own transaction (new branch, sent to the Contact); a non-2xx
        ACK reuses the INVITE's branch and Request-URI."""
        d = self._d
        cseq = h.get("cseq", "1").split()[0]
        to = h.get("to", d["remote"])
        branch = "z9hG4bK" + uuid.uuid4().hex if success else d["branch"]
        lines = [f"ACK {d['ruri']} SIP/2.0", f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch={branch};rport",
                 f"From: {d['local']}", f"To: {to}", f"Call-ID: {d['call_id']}", f"CSeq: {cseq} ACK",
                 "Max-Forwards: 70", "Content-Length: 0"]
        self._send(("\r\n".join(lines) + "\r\n\r\n").encode(), d["peer"])

    # ---------------------------------------------------------------- incoming requests

    def _on_sip(self, data: bytes, addr) -> None:
        start, h, body = parse_message(data)
        if start.startswith("SIP/2.0"):
            self._on_response(start, h, body)
            return
        method = start.split(" ", 1)[0].upper()
        d = self._d
        in_dialog = h.get("call-id") == d.get("call_id") and self.state in ("up", "calling")
        if method == "OPTIONS":
            self._reply(h, addr, 200, "OK")
        elif method == "BYE":
            if not in_dialog:
                self._reply(h, addr, 481, "Call/Transaction Does Not Exist")
            else:
                self._reply(h, addr, 200, "OK")
                self._end("remote hangup")
        elif method == "CANCEL":
            self._reply(h, addr, 200, "OK")
        elif method == "ACK":
            pass
        elif method == "INVITE" and in_dialog and self.state == "up":
            # re-INVITE (session refresh, media change): keep our codec, follow a new address.
            remote, _ = parse_sdp(body)
            if remote and self.rtp:
                self.rtp.remote = remote
            self._reply(h, addr, 200, "OK", build_sdp(self.local_ip, self.media_port, [self.rtp.codec]))
        elif method == "INVITE":
            self._incoming(h, body, addr)
        else:
            self._reply(h, addr, 405, "Method Not Allowed")

    def _incoming(self, h: dict[str, str], body: str, addr) -> None:
        if self.target is not None or self.state != "idle":
            self._reply(h, addr, 486, "Busy Here")
            return
        remote, offered = parse_sdp(body)
        codec = match_codec(offered, self.codecs)
        if remote is None or codec is None:
            self._reply(h, addr, 488, "Not Acceptable Here")
            return
        tag = uuid.uuid4().hex[:10]
        self._d = dict(call_id=h.get("call-id"), cseq=0, local=f"{h.get('to', '')};tag={tag}",
                       remote=h.get("from", ""), ruri=_uri_in(h.get("contact", "")) or f"sip:{addr[0]}:{addr[1]}",
                       peer=addr)
        self._reply(h, addr, 200, "OK", build_sdp(self.local_ip, self.media_port, [codec]), tag=tag)
        self._start_media(codec, remote)

    def _reply(self, h: dict[str, str], addr, code: int, reason: str, sdp: str | None = None,
               tag: str | None = None) -> None:
        to = h.get("to", "")
        if tag and "tag=" not in to:
            to += f";tag={tag}"
        lines = [f"SIP/2.0 {code} {reason}", f"Via: {h.get('via', '')}", f"From: {h.get('from', '')}", f"To: {to}",
                 f"Call-ID: {h.get('call-id', '')}", f"CSeq: {h.get('cseq', '')}"]
        if sdp is not None:
            lines += [f"Contact: <sip:{self.from_user}@{self.local_ip}:{self.sip_port}>",
                      "Content-Type: application/sdp"]
        body = sdp or ""
        lines.append(f"Content-Length: {len(body.encode())}")
        self._send(("\r\n".join(lines) + "\r\n\r\n" + body).encode(), addr)

    # ---------------------------------------------------------------- dialog helpers

    def _request(self, method: str, ruri: str, extra: list[str], body: str = "", branch: str | None = None) -> bytes:
        d = self._d
        branch = branch or "z9hG4bK" + uuid.uuid4().hex
        lines = [f"{method} {ruri} SIP/2.0", f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch={branch};rport",
                 f"From: {d['local']}", f"To: {d['remote']}", f"Call-ID: {d['call_id']}",
                 f"CSeq: {d['cseq']} {method}", "Max-Forwards: 70", "User-Agent: ham-modem", *extra,
                 f"Content-Length: {len(body.encode())}"]
        return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()

    def _send_bye(self) -> None:
        d = self._d
        if not d:
            return
        d["cseq"] = d.get("cseq", 0) + 1
        self._send(self._request("BYE", d["ruri"], []), d["peer"])

    def _send_cancel(self) -> None:
        d = self._d
        if not d or not self._last_invite:
            return
        lines = [f"CANCEL {d['ruri']} SIP/2.0",
                 f"Via: SIP/2.0/UDP {self.local_ip}:{self.sip_port};branch={d['branch']};rport",
                 f"From: {d['local']}", f"To: {d['remote']}", f"Call-ID: {d['call_id']}",
                 f"CSeq: {d['cseq']} CANCEL", "Max-Forwards: 70", "Content-Length: 0"]
        self._send(("\r\n".join(lines) + "\r\n\r\n").encode(), d["peer"])

    def _start_media(self, codec: Codec, remote: tuple[str, int]) -> None:
        self.rtp = RtpSession(self.media, codec, remote, frame_ms(codec.rate))
        self.calls += 1
        self._set_state("up", f"{codec.name}/{codec.rate} media {remote[0]}:{remote[1]}")

    def _end(self, why: str) -> None:
        self.rtp = None
        self._set_state("idle", why)
        self._next_action = time.monotonic() + (1.0 if self.target else 0)

    def hangup(self) -> None:
        if self.state == "up":
            self._send_bye()
            self._end("local hangup")

    def _set_state(self, state: str, detail: str) -> None:
        self.state = state
        log.info("%s: %s (%s)", self.name, state, detail)
        if self.on_state:
            self.on_state(state, detail)

    def _send(self, data: bytes, addr) -> None:
        try:
            self.sip.sendto(data, addr)
        except OSError as exc:
            log.debug("SIP send to %s failed: %s", addr, exc)
