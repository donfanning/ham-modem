"""ARDOP ARQ session layer: one station's protocol state machine, driven by its audio stream.

Ported for interoperability from ardopcf's ARQ.c / SoundInput.c / ARDOPC.c (MIT; Muething, Wiseman, LaRue), with
the protocol rules of the ARDOP specification (Appendix D). The station is clocked by the samples passing through
``process()``, so two stations can be wired together offline (tests) or put on a live audio leg; it never reads a
wall clock.

States (ardopcf's names): DISC, ISS (sending), IRS (receiving), IDLE (ISS with nothing to send), IRStoISS (IRS
sending BREAKs to take over). Sub-states: ISSConReq, ISSConAck, ISSData, IRSConAck, IRSData, IRSfromISS.

Connection (rule 1): the caller repeats ConReq (every 2 s, 5 tries); the callee answers ConAck<bw> carrying the
caller's received leader length; the caller answers with its own ConAck; the callee ACKs and both are connected.
Data (rule 2): the ISS sends data frames alternating even/odd; the IRS ACKs (passing new data to the host once) or
NAKs, with a 5-bit decode quality the ISS averages to shift between faster and more robust modes (ardopcf's
Gearshift_9). Multi-carrier frames keep correctly decoded carriers across repeats (memory ARQ). With nothing to
send the ISS repeats IDLE; an IRS with data answers IDLE with BREAK and becomes the ISS once ACKed (rule 3). Either
side ends with DISC, answered by END (rules 1.6, 1.7); silence for the session timeout ends it too (rule 1.8).

Host events use ardopcf's host-interface strings ("NEWSTATE IRS", "CONNECTED K1ABC 500", "BUFFER 0", ...); data
received in sequence is appended to ``received``.
"""

from __future__ import annotations

import random
from collections import deque
from typing import Callable

import numpy as np

from . import codec, modulate
from .demod import Decoded
from .stream import Phy

# Data modes per session bandwidth, most robust first, and the average quality needed to shift up from each
# (ardopcf's DataModes*/byt* tables; 2000 Hz: the HF list, used unless 600 baud FM modes are enabled).
DATA_MODES = {200: (0x48, 0x42, 0x40, 0x44, 0x46),
              500: (0x48, 0x42, 0x40, 0x50, 0x52, 0x54),
              1000: (0x4C, 0x4A, 0x50, 0x60, 0x62, 0x64),
              2000: (0x4C, 0x4A, 0x50, 0x60, 0x70, 0x72, 0x74)}
DATA_MODES_FM2000 = (0x4C, 0x4A, 0x7C, 0x7A)
SHIFT_UP = {200: (82, 84, 84, 85, 0), 500: (80, 84, 84, 75, 79, 0), 1000: (80, 80, 80, 80, 75, 0),
            2000: (80, 80, 80, 76, 85, 75, 0)}
SHIFT_UP_FM2000 = (60, 85, 85, 0)

FRAME_BY_NAME = {f.name: t for t, f in codec.FRAMES.items()}
FEC_PAUSE_MS = 400            # gap between FEC frames and between a frame and its repeats
CONREQ_CODES = set(codec.CONREQ.values())
CONACK_CODES = {v: k for k, v in codec.CONACK.items()}
REPLY_DELAY_MS = 250          # ardopcf waits this long after a decode before answering
PENDING_TIMEOUT_MS = 10_000   # IRS: ConAck sent, waiting for the caller's ConAck
FINAL_ID_MS = 3000
DISC_FALLBACK_MS = 6000       # pending DISCONNECT with nothing heard for this long: send DISC anyway


def is_data(t: int) -> bool:
    return 0x40 <= t <= 0x7F and t in codec.FRAMES


def frame_size(t: int) -> int:
    info = codec.FRAMES[t]
    return info.data_len * info.carriers


def negotiate_bw(setting: str, conreq: int) -> int | None:
    """IRS side (ardopcf IRSNegotiateBW): session bandwidth for a ConReq under our ARQBW setting, or None."""
    want = int(setting.rstrip("AXFORCEDM"))
    forced = setting.endswith("FORCED")
    name = next(k for k, v in codec.CONREQ.items() if v == conreq)
    req, req_forced = int(name[:-1]), name.endswith("F")
    if forced:
        return want if (req >= want and not req_forced) or (req_forced and req == want) else None
    if req_forced:
        return req if req <= want else None
    return min(req, want)


class ArqStation:
    def __init__(self, mycall: str, fs: int = codec.SAMPLE_RATE, bandwidth: str = "500MAX", grid: str = "",
                 leader_ms: int = 240, trailer_ms: int = 20, timeout_s: int = 120, conreq_repeats: int = 5,
                 autobreak: bool = True, fast_start: bool = True, listen: bool = True, fm600: bool = False,
                 drive: float = 1.0, seed: int | None = None,
                 on_event: Callable[[str], None] | None = None,
                 on_data: Callable[[str, bytes], None] | None = None) -> None:
        self.mycall, self.grid, self.bandwidth = mycall.upper(), grid, bandwidth
        self.fs = fs
        self.phy = Phy(fs)
        self.leader_ms, self.trailer_ms = leader_ms, trailer_ms
        self.drive = drive                               # transmit level (ardopcf DRIVELEVEL / 100)
        self.timeout_ms, self.conreq_repeats = timeout_s * 1000, conreq_repeats
        self.autobreak, self.fast_start, self.listen, self.fm600 = autobreak, fast_start, listen, fm600
        self.rng = random.Random(seed)
        self.on_event = on_event
        self.on_data = on_data                           # (tag, bytes): "ARQ" data in sequence, "IDF" ID frames
        self.events: list[str] = []
        self.trace: list[tuple] = []                     # (ms, frame, ok, session, accepted) for every frame heard
        self.received = bytearray()
        self.now = 0.0                                   # ms of audio processed
        # transmit side
        self._txq: deque = deque()                       # (encoded, leader_ms) waiting to be modulated
        self._tx: np.ndarray | None = None               # samples being played
        self._tx_pos = 0
        self._not_before = 0.0                           # earliest start of the next transmission
        self._last: tuple[bytes, int] | None = None      # last frame sent (repeats)
        self.ptt = False
        self.outq = bytearray()
        self.state, self.sub = "DISC", "None"
        self.stats = {"data_frames_sent": 0, "repeats": 0, "acks": 0, "naks": 0, "shift_up": 0, "shift_down": 0}
        self.modes_used: dict[str, int] = {}
        self._reset_connection()
        self.last_session = 0xFF
        self.final_id_at = 0.0
        self._final_id = ""
        self.ping_target, self.ping_repeats = "", 0
        # FEC (broadcast) mode
        self.protocol_mode = "ARQ"
        self.fec_mode, self.fec_repeats, self.fec_id = "4FSK.500.100", 0, False
        self._fec_type = self._fec_sent = self._fec_repeated = 0
        self._fec_id_pending = False
        self._fec_last_ok: tuple[int, int] | None = None   # (frame type, CRC) of the last frame passed up
        self._fec_failed: tuple[int, bytes] | None = None

    # ================================================================== host interface

    def connect(self, target: str, bandwidth: str | None = None) -> None:
        """ARQCALL: send ConReq (repeated) to ``target``."""
        self._reset_connection()
        self.remote, self.local = target.upper(), self.mycall
        bw = bandwidth or self.bandwidth
        key = f"{int(bw.rstrip('AXFORCEDM'))}{'F' if bw.endswith('FORCED') else 'M'}"
        self.session = self.pending_session = codec.session_id(self.local, self.remote)
        self.pending = True
        self._timeout_trip = self.now
        self._set_state("ISS")
        self.sub = "ISSConReq"
        self.repeat_count = 1
        self.repeat_interval = 2000
        self.rpt = True
        self._send(modulate.encode_conreq(self.local, self.remote, key))

    def disconnect(self) -> None:
        if self.state in ("IDLE", "IRS", "ISS", "IRStoISS"):
            self.disc_request = True
            self._disc_requested_at = self.now
            if self.state in ("ISS", "IDLE") and self._tx is None and not self._txq:
                self._check_disconnect()

    def purge(self) -> None:
        self._clear_outq()
        self._event("BUFFER 0")

    def abort(self) -> None:
        self.rpt = self.disc_repeating = False
        self._txq.clear()
        self._clear_outq()
        self._set_state("DISC")
        self._reset_connection()

    def send(self, data: bytes) -> None:
        self.outq += data                                # in IDLE, sent when the IRS ACKs the next IDLE
        self._event(f"BUFFER {len(self.outq)}")

    def break_(self) -> None:
        if self.state == "IRS":
            self.break_cmd = True

    def ping(self, target: str, count: int = 5) -> None:
        if self.state == "DISC":
            self.ping_target, self.ping_repeats, self.repeat_count = target.upper(), count, 1
            self.repeat_interval = 2000
            self.rpt = True
            self._send(modulate.encode_ping(self.mycall, self.ping_target))

    def fec_start(self) -> bool:
        """FECSEND TRUE: broadcast the queued data (ardopcf StartFEC); False when there is nothing to send."""
        if self.state == "FECSEND":
            return True
        if not self.outq or self.fec_mode + ".E" not in FRAME_BY_NAME:
            return False
        self._fec_sent = self._fec_repeated = 0
        self._fec_id_pending = self.fec_id
        self._set_state("FECSEND")
        self.next_play = self.now
        return True

    def fec_abort(self) -> None:
        if self.state == "FECSEND":
            self._txq.clear()
            self._clear_outq()
            self._set_state("DISC")

    def send_id(self, call: str | None = None) -> None:
        self._send(modulate.encode_idframe(call or self.mycall, self.grid), self.leader_ms)

    # ================================================================== audio

    def process(self, rx: np.ndarray) -> np.ndarray:
        """Consume one block of received audio (at ``fs``); return the block to transmit (zeros when not keyed)."""
        n = len(rx)
        self.phy.set_sessions(self._sessions())
        if self._tx is None:
            frames = self.phy.feed(rx)
        else:
            frames = self.phy.feed(np.zeros(n))          # half duplex: deaf while keyed
        self.now += n * 1000 / self.fs
        for d in frames:
            self._on_frame(d)
        self._timers()
        return self._play(n)

    def _play(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.int16)
        filled = 0
        while filled < n:
            if self._tx is None:
                if not self._txq or self.now < self._not_before or self.phy.busy():
                    break
                encoded, leader = self._txq.popleft()
                self._tx = self.phy.transmit(encoded, leader, self.trailer_ms)
                if self.drive != 1.0:
                    self._tx = np.round(self._tx * self.drive).astype(np.int16)
                self._tx_pos = 0
                if not self.ptt:
                    self.ptt = True
                    self._event("PTT TRUE")
            k = min(n - filled, len(self._tx) - self._tx_pos)
            out[filled:filled + k] = self._tx[self._tx_pos:self._tx_pos + k]
            filled += k
            self._tx_pos += k
            if self._tx_pos >= len(self._tx):
                self._tx = None
                if not self._txq:
                    self.ptt = False
                    self._event("PTT FALSE")
                    self._tx_end = self.now
                    if self.state == "FECSEND":
                        self.next_play = self.now + FEC_PAUSE_MS
                    elif self.rpt or self.disc_repeating:
                        self.next_play = self.now + self.repeat_interval
        return out

    @property
    def keyed(self) -> bool:
        return self._tx is not None or bool(self._txq)

    # ================================================================== plumbing

    def _event(self, text: str) -> None:
        self.events.append(text)
        if self.on_event:
            self.on_event(text)

    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        if state == "DISC":
            self.disc_request = False
            self.connected = self.pending = False
            self._clear_outq()
        elif state in ("ISS", "IDLE"):
            self.rpt = False
        elif state in ("IRS", "IRStoISS"):
            self.last_acked_rx_type = 0
        self._event(f"NEWSTATE {state}")

    def _sessions(self) -> tuple[int, ...]:
        s = {self.session, self.pending_session, self.last_session} - {0xFF}
        return tuple(sorted(s))

    def _send(self, encoded: bytes, leader: int | None = None) -> None:
        leader = self.leader_ms if leader is None else leader
        self._last = (encoded, leader)
        self._txq.append((encoded, leader))

    def _reply(self, encoded: bytes, leader: int | None = None) -> None:
        self._send(encoded, leader)

    def _ack(self, quality: int) -> None:
        self._reply(modulate.encode_dataack(quality, self.session))

    def _clear_outq(self) -> None:
        if self.outq:
            self.outq.clear()
        self.inflight = 0

    def _reset_connection(self) -> None:
        """ardopcf InitializeConnection + per-session counters."""
        self.remote = self.local = ""
        self.session = self.pending_session = 0xFF
        self.session_bw = 0
        self.connected = self.pending = False
        self.rpt = self.disc_repeating = self.disc_request = self.break_cmd = False
        self._disc_requested_at = 0.0
        self._last_heard = self.now
        self.repeat_count, self.repeat_interval, self.next_play = 0, 2000, 0.0
        self.rmt_leader = 0
        self.received_leader = 0
        self.last_to_host = -1
        self.last_acked_rx_type = 0
        self.last_sent_type = self.last_acked_type = 0
        self.last_frame_data = False
        self.inflight = 0
        self.modes, self.thresholds, self.mode_ptr, self.current_type = (), (), 0, 0
        self.avg_quality, self.shift, self.ack_ctr, self.nak_ctr = 0, 0, 0, 0
        self.worked, self.tried = [0] * 16, [0] * 16
        self.pending_timeout = 0.0
        self.timeout_triggered, self.timeout_send_at = False, 0.0
        self._timeout_trip = self.now
        self._mem_type, self._mem_blocks = -1, []

    # ================================================================== timers (ardopcf CheckTimers)

    def _fec_step(self) -> None:
        """ardopcf GetNextFECFrame: the next new frame, or a repeat of the last one (400 ms apart)."""
        if self._fec_id_pending:
            self._fec_id_pending = False
            self.send_id(self.mycall)
            return
        if self._fec_sent and self._fec_repeated < self.fec_repeats:
            self._fec_repeated += 1
            self._txq.append(self._last)
            return
        if not self.outq:
            self._set_state("DISC")
            return
        if self._fec_sent:
            self._fec_type ^= 1
        else:
            self._fec_type = FRAME_BY_NAME[self.fec_mode + ".E"]
        n = min(frame_size(self._fec_type), len(self.outq))
        chunk = bytes(self.outq[:n])
        del self.outq[:n]
        self._event(f"BUFFER {len(self.outq)}")
        self._fec_sent += 1
        self._fec_repeated = 0
        self._send(modulate.encode_fsk_data(self._fec_type, chunk, 0xFF))

    def _fec_receive(self, t: int, d: Decoded, ok: bool, data: bytes) -> None:
        """ardopcf ProcessRcvdFECDataFrame: good frames go up as FEC (repeats dropped), the last bad one as ERR."""
        if ok:
            self._mem_type, self._mem_blocks = -1, []
            key = (t, codec.crc16(data))
            if key == self._fec_last_ok:
                return
            if self._fec_failed and self._fec_failed[0] != t:
                self._fec_flush_failed()
            self._fec_last_ok = key
            self._fec_failed = None
            if self.on_data:
                self.on_data("FEC", data)
        else:
            if self._fec_failed and self._fec_failed[0] != t:
                self._fec_flush_failed()
            if data:
                self._fec_failed = (t, data)

    def _fec_flush_failed(self) -> None:
        if self._fec_failed and self.on_data:
            self.on_data("ERR", self._fec_failed[1])
        self._fec_failed = None

    def _timers(self) -> None:
        if self.state == "FECSEND":
            if not self.keyed and not self.phy.busy() and self.now >= self.next_play:
                self._fec_step()
            return
        if (self.rpt or self.disc_repeating) and not self.keyed and self.now > self.next_play \
                and not self.phy.busy():
            if self._next_arq_frame():
                self.stats["repeats"] += 1
                self._txq.append(self._last)
            else:
                self.rpt = False
        # An IRS only sends DISC in reply to IDLE or data (as ardopcf); if the ISS has gone quiet, do not wait for
        # the session timeout.
        if self.disc_request and not self.keyed and not self.phy.busy() \
                and self.now - max(self._disc_requested_at, self._last_heard) > DISC_FALLBACK_MS:
            self._check_disconnect()
        if self.state in ("ISS", "IDLE", "IRS", "IRStoISS") and not self.timeout_triggered \
                and self.now - self._timeout_trip > self.timeout_ms:
            self.rpt = False
            self.timeout_triggered = True
            self.timeout_send_at = self.now + 1000
        if self.timeout_send_at and self.now > self.timeout_send_at:
            self.timeout_send_at = 0.0
            self.send_id(self.local or self.mycall)
            self._event("DISCONNECTED")
            self._event(f"STATUS ARQ Timeout from Protocol State:  {self.state}")
            self.rpt = False
            self._send(modulate.encode_control(codec.DISC, self.session))
            self._set_state("DISC")
            self._reset_connection()
        if self.pending_timeout and self.now > self.pending_timeout:
            self.pending_timeout = 0.0
            self._event("DISCONNECTED")
            self._event(f"STATUS ARQ CONNECT REQUEST TIMEOUT FROM PROTOCOL STATE: {self.state}")
            self.rpt = False
            self.state = "DISC"
            self._reset_connection()
        if self.final_id_at and self.now > self.final_id_at and not self.keyed:
            self.final_id_at = 0.0
            self.send_id(self._final_id or self.mycall)

    def _next_arq_frame(self) -> bool:
        """ardopcf GetNextARQFrame: True to repeat the last frame."""
        if self.disc_repeating:
            self.repeat_count += 1
            if self.repeat_count > 5:
                self._event("DISCONNECTED")
                self._event(f"STATUS END NOT RECEIVED CLOSING ARQ SESSION WITH {self.remote}")
                self.disc_repeating = False
                self._set_state("DISC")
                self._reset_connection()
                return False
            self._last = (modulate.encode_control(codec.DISC, self.session), self.leader_ms)
            return True
        if self.state in ("ISS", "IDLE") and self._check_disconnect():
            return False
        if self.state == "ISS" and self.sub == "ISSConReq":
            self.repeat_count += 1
            if self.repeat_count > self.conreq_repeats:
                self._event(f"STATUS CONNECT TO {self.remote} FAILED!")
                self._set_state("DISC")
                self._reset_connection()
                return False
            return True
        if self.state == "DISC" and self.ping_repeats:
            self.repeat_count += 1
            if self.repeat_count <= self.ping_repeats:
                return True
            self.ping_repeats = 0
            return False
        if self.state == "DISC":
            return False
        return self.rpt

    def _check_disconnect(self) -> bool:
        if not self.disc_request:
            return False
        self._event("STATUS INITIATING ARQ DISCONNECT")
        self.repeat_interval, self.repeat_count = 2000, 1
        self.disc_request, self.disc_repeating, self.rpt = False, True, False
        self._send(modulate.encode_control(codec.DISC, self.session))
        return True

    def _interval(self, ms: int) -> int:
        return max(1000, ms + self.rmt_leader)

    # ================================================================== ISS data (SendData / GetNextFrameData)

    def _init_modes(self) -> None:
        if self.session_bw == 2000 and self.fm600:
            self.modes, self.thresholds = DATA_MODES_FM2000, SHIFT_UP_FM2000
        else:
            self.modes, self.thresholds = DATA_MODES[self.session_bw], SHIFT_UP[self.session_bw]
        self.mode_ptr = len(self.modes) // 2 if self.fast_start else 0
        self.current_type = self.modes[self.mode_ptr]
        self.shift = 0

    def _next_frame(self) -> tuple[int, int]:
        if self.shift < 0 and self.mode_ptr > 0:
            self.mode_ptr = max(0, self.mode_ptr + self.shift)
        elif self.shift > 0 and self.mode_ptr < len(self.modes) - 1:
            self.mode_ptr = min(len(self.modes) - 1, self.mode_ptr + self.shift)
        self.shift = 0
        self.current_type = self.modes[self.mode_ptr]
        t = self.current_type
        if (t & 1) == (self.last_acked_type & 1):
            t ^= 1
        self.last_sent_type = t
        return t, min(frame_size(t), len(self.outq))

    def _send_data(self) -> None:
        if self.disc_repeating:
            return
        if self.state == "IDLE":
            return
        if self.state != "ISS":
            return
        if self._check_disconnect():
            return
        if self.outq:
            t, n = self._next_frame()
            self.inflight = n
            self.last_frame_data = True
            self.repeat_interval = self._interval({1: 1500, 2: 1700, 4: 1900, 8: 2100}[codec.FRAMES[t].carriers])
            self._timeout_trip = self.now
            self.rpt = True
            self.sub = "ISSData"
            self.stats["data_frames_sent"] += 1
            name = codec.FRAMES[t].name[:-2]
            self.modes_used[name] = self.modes_used.get(name, 0) + 1
            self._send(modulate.encode_fsk_data(t, bytes(self.outq[:n]), self.session))
        else:
            self._set_state("IDLE")
            self.rpt = True
            self._timeout_trip = self.now
            self.last_frame_data = False
            self.repeat_interval = self._interval(2000)
            self._send(modulate.encode_control(codec.IDLE, self.session))

    def _quality_avg(self, q: int) -> None:
        self.avg_quality = q if self.avg_quality == 0 else int(self.avg_quality * 0.5 + q * 0.5 + 0.5)

    def _gearshift(self) -> None:
        """ardopcf Gearshift_9."""
        p = self.mode_ptr
        down_naks = 2 if self.worked[p] else 1
        if self.ack_ctr:
            self.worked[p] += 1
        if p > 0 and self.nak_ctr >= down_naks:
            self.shift = -1
            self.avg_quality = self.nak_ctr = self.ack_ctr = 0
            self.stats["shift_down"] += 1
        elif self.avg_quality > self.thresholds[p] and p < len(self.modes) - 1 and self.ack_ctr >= 2:
            if len(self.outq) <= frame_size(self.modes[p]):
                self.shift = 0
                return
            if self.tried[p + 1] and not self.worked[p + 1] and self.ack_ctr < 5:
                self.shift = 0
                return
            self.shift = 1
            self.tried[p + 1] = 1
            self.avg_quality = self.nak_ctr = self.ack_ctr = 0
            self.stats["shift_up"] += 1

    # ================================================================== received frames

    def _on_frame(self, d: Decoded) -> None:
        t = d.frame_type
        if t < 0:
            return
        if self.state == "DISC" and d.ok and (t in CONREQ_CODES or t == codec.PING):
            self._event("PENDING")
        self._not_before = self.now + REPLY_DELAY_MS
        ok = d.ok
        if is_data(t):
            ok, data = self._memory_arq(d)
        else:
            data = d.data
        # session filter: connected/pending frames must carry our session; unconnected ones 0xFF
        expected = {self.session, self.pending_session} - {0xFF}
        if self.state == "DISC":
            accept = d.session == 0xFF or (t == codec.DISC and d.session == self.last_session)
        else:
            accept = not expected or d.session in expected or (t in CONREQ_CODES and d.session == 0xFF)
        self.trace.append((round(self.now), d.name, ok, d.session, accept, self.state, self.sub))
        if not accept:
            return
        self._last_heard = self.now
        if self.state == "FECSEND":
            return
        if self.protocol_mode == "FEC" and self.state == "DISC" and is_data(t) and d.session == 0xFF:
            self._fec_receive(t, d, ok, data or b"")
            return
        if self.disc_repeating and ok and t == codec.END and self.state != "DISC":
            self._end_session(False, True, f"STATUS ARQ CONNECTION ENDED WITH {self.remote}")
            return
        if t == codec.IDFRAME:
            if ok:
                text = f"ID:{d.extra.get('caller')} [{d.extra.get('grid', '')}]"
                self._event(f"STATUS IDFRAME {d.extra.get('caller')} {d.extra.get('grid', '')}".rstrip())
                if self.on_data:
                    self.on_data("IDF", text.encode())
            return
        # SoundInput.c shortcuts
        if self.state == "IRStoISS":
            if t >= codec.DATAACK_MIN and ok:
                self.rpt = False
                self.last_to_host = -1
                if not self.modes:
                    self._init_modes()
                self._set_state("ISS")
                self.sub = "ISSData"
                self._send_data()
            elif is_data(t):
                self.repeat_interval = self._interval(1000 + self.rng.randrange(2000))
                self.rpt = True
                self._reply(modulate.encode_control(codec.BREAK, self.session))
            return
        if is_data(t) and self.break_cmd and self.state == "IRS" and self.sub == "IRSData" \
                and t != self.last_acked_rx_type:
            self._go_irs_to_iss()
            return
        self._process(t, d, ok, data)

    def _memory_arq(self, d: Decoded) -> tuple[bool, bytes]:
        blocks = d.extra.get("blocks") or []
        if d.frame_type != self._mem_type or len(self._mem_blocks) != len(blocks):
            self._mem_type, self._mem_blocks = d.frame_type, list(blocks)
        else:
            self._mem_blocks = [a if a is not None else b for a, b in zip(self._mem_blocks, blocks)]
        if blocks and all(b is not None for b in self._mem_blocks):
            return True, b"".join(self._mem_blocks)
        return d.ok, d.data or b""

    def _go_irs_to_iss(self) -> None:
        self._timeout_trip = self.now
        self.break_cmd = False
        self.rpt = True
        self.repeat_interval = self._interval(1000 + self.rng.randrange(2000))
        self._set_state("IRStoISS")
        self._event("STATUS QUEUE BREAK new Protocol State IRStoISS")
        self._reply(modulate.encode_control(codec.BREAK, self.session))

    def _connected_event(self) -> None:
        self._event(f"CONNECTED {self.remote} {self.session_bw}")

    def _end_session(self, send_end: bool, send_id: bool, status: str) -> None:
        self._event("DISCONNECTED")
        self._event(status)
        self.last_session = self.session
        self.disc_repeating = False
        self.rpt = False
        self._final_id = self.local
        if send_end:
            self.final_id_at = self.now + FINAL_ID_MS
            self._reply(modulate.encode_control(codec.END, self.session))
        self._set_state("DISC")
        local = self.local
        self._reset_connection()
        if send_id:
            self.send_id(local or self.mycall)

    def _process(self, t: int, d: Decoded, ok: bool, data: bytes | None) -> None:
        """ardopcf ProcessRcvdARQFrame."""
        st = self.state
        if st == "DISC":
            if ok and t == codec.DISC:
                self.final_id_at = self.now + FINAL_ID_MS
                self._reply(modulate.encode_control(codec.END, self.last_session))
                return
            if ok and t == codec.PING:
                if self.listen and d.extra.get("target", "").upper() == self.mycall:
                    self._reply(modulate.encode_pingack(d.extra.get("snr_db", 0), d.extra.get("quality", 0)))
                    self._event("PINGREPLY")
                return
            if ok and t == codec.PINGACK and self.ping_repeats:
                v = d.extra.get("value", 0)
                self._event(f"PINGACK {(v >> 3) - 10} {30 + 10 * (v & 7)}")
                self.ping_repeats, self.rpt = 0, False
                return
            if not ok or t not in CONREQ_CODES or not self.listen:
                return
            if d.extra.get("target", "").upper() != self.mycall:
                self._event("CANCELPENDING")
                return
            self._answer_conreq(t, d)
            return

        if st == "IRS":
            if self.sub == "IRSConAck":
                if not ok:
                    return
                if t in CONREQ_CODES:
                    if self.listen and d.extra.get("target", "").upper() == self.mycall:
                        self._answer_conreq(t, d)
                    return
                if t in CONACK_CODES:
                    self.session_bw = CONACK_CODES[t]
                    self.session = self.pending_session
                    self.connected, self.pending = True, False
                    self.pending_timeout = 0.0
                    self._timeout_trip = self.now
                    self.sub = "IRSData"
                    self.last_to_host = -1
                    self.nak_ctr = 0
                    self.rpt = False
                    self._connected_event()
                    self._ack(self._last_quality(d))
                    self._init_modes()
                return
            if self.sub in ("IRSData", "IRSfromISS"):
                if t in CONACK_CODES:
                    self._timeout_trip = self.now
                    self._ack(self._last_quality(d))
                    return
                if ok and t == codec.DISC:
                    self._end_session(True, False, f"STATUS ARQ CONNECTION ENDED WITH {self.remote}")
                    return
                if ok and t == codec.END:
                    self._end_session(False, True, f"STATUS ARQ CONNECTION ENDED WITH {self.remote}")
                    return
                if ok and t == codec.BREAK:
                    self.rpt = False
                    self._ack(100)
                    self._timeout_trip = self.now
                    return
                if ok and t == codec.IDLE:
                    self.rpt = False
                    if self._check_disconnect():
                        return
                    if (self.autobreak and self.outq) or self.break_cmd:
                        self._go_irs_to_iss()
                    else:
                        self._timeout_trip = self.now
                        self._ack(100)
                    return
                if is_data(t) and self.disc_request:
                    if ok and t != self.last_to_host:            # deliver it, then DISC instead of the ACK
                        self.received += data or b""
                        if self.on_data and data:
                            self.on_data("ARQ", bytes(data))
                        self.last_to_host = t
                    self._check_disconnect()
                    return
                if ok and is_data(t):
                    if not self.rmt_leader:
                        self.rmt_leader = d.extra.get("leader_ms", 0) + 200
                    if t != self.last_to_host:
                        self.received += data or b""
                        if self.on_data and data:
                            self.on_data("ARQ", bytes(data))
                        self.last_to_host = t
                        self._timeout_trip = self.now
                    if self.sub == "IRSfromISS":
                        self.sub = "IRSData"
                    self.rpt = False
                    self._ack(self._last_quality(d))
                    self.last_acked_rx_type = t
                    return
                if not ok and t == self.last_acked_rx_type:
                    self.rpt = False
                    self._ack(self._last_quality(d))
                    return
                if not ok and is_data(t):
                    if self.sub == "IRSfromISS":
                        self.sub = "IRSData"
                    self.rpt = False
                    self._reply(modulate.encode_dataack(self._last_quality(d), self.session, nak=True))
                return
            return

        if st == "IDLE":
            if not ok:
                return
            if t >= codec.DATAACK_MIN:
                if self.outq:
                    self._set_state("ISS")
                    self.sub = "ISSData"
                    self._send_data()
                return
            if t == codec.BREAK:
                self._timeout_trip = self.now
                self.rpt = False
                self._ack(100)
                self._event("STATUS BREAK received from Protocol State IDLE, new state IRS")
                self._become_irs()
                return
            if t == codec.DISC:
                self._end_session(True, False, f"STATUS ARQ CONNECTION ENDED WITH {self.remote} ")
                return
            if t == codec.END:
                self._end_session(False, True, f"STATUS ARQ CONNECTION ENDED WITH {self.remote} ")
            return

        if st == "ISS":
            if self.sub == "ISSConReq":
                if not ok:
                    return
                if t in CONACK_CODES:
                    self.session_bw = CONACK_CODES[t]
                    self._init_modes()
                    self.received_leader = d.extra.get("leader_ms", 0)
                    self.repeat_interval = 2000
                    self.rpt = True
                    self.sub = "ISSConAck"
                    self._reply(modulate.encode_conack(self.session_bw, self.received_leader, self.session))
                    return
                if t in (codec.CONREJBUSY, codec.CONREJBW):
                    word = "BUSY" if t == codec.CONREJBUSY else "BW"
                    self._event(f"REJECTED{word} {self.remote}")
                    self.abort()
                return
            if self.sub == "ISSConAck":
                if (ok and t >= codec.DATAACK_MIN) or t == codec.BREAK:
                    if not self.rmt_leader:
                        self.rmt_leader = d.extra.get("leader_ms", 0) + 200
                    self.avg_quality = 0
                    self.rpt = False
                    self.connected, self.pending = True, False
                    self.last_acked_type = 1                    # first data frame is even
                    self._connected_event()
                    self.sub = "ISSData"
                    self.nak_ctr = 0
                    if t == codec.BREAK and not self.outq:
                        self._clear_outq()
                        self._ack(100)
                        self._timeout_trip = self.now
                        self._become_irs()
                    else:
                        self._send_data()
                    return
                if ok and t in (codec.CONREJBUSY, codec.CONREJBW):
                    self._event(f"REJECTED{'BUSY' if t == codec.CONREJBUSY else 'BW'} {self.remote}")
                    self._set_state("DISC")
                    self._reset_connection()
                return
            if self.sub == "ISSData":
                if self._check_disconnect():
                    return
                if not ok:
                    return
                if t >= codec.DATAACK_MIN:
                    self._timeout_trip = self.now
                    self.stats["acks"] += 1
                    if self.last_frame_data:
                        self.ack_ctr += 1
                        self.last_acked_type = self.last_sent_type
                        if self.inflight:
                            del self.outq[:self.inflight]
                            self.inflight = 0
                            self._event(f"BUFFER {len(self.outq)}")
                        self._quality_avg(38 + 2 * (t - codec.DATAACK_MIN))
                        self._gearshift()
                    self.nak_ctr = 0
                    self.rpt = False
                    self._send_data()
                    return
                if t == codec.BREAK:
                    if not self.connected:
                        self.connected = True
                        self._connected_event()
                    self._clear_outq()                          # ardopcf saves it for RESTOREBUFFER
                    self.rpt = False
                    self.nak_ctr = 0
                    self._event("STATUS BREAK received from Protocol State ISS, new state IRS")
                    self._ack(100)
                    self._timeout_trip = self.now
                    self._become_irs()
                    return
                if t <= codec.DATANAK_MAX:
                    self.stats["naks"] += 1
                    if self.last_frame_data:
                        self.nak_ctr += 1
                        self._quality_avg(38 + 2 * t)
                        self._gearshift()
                        if self.shift != 0:
                            self._timeout_trip = self.now
                            self.nak_ctr = 0
                            self._send_data()
                        self.ack_ctr = 0
                    return
                if t == codec.DISC:
                    self._end_session(True, False, f"STATUS ARQ CONNECTION ENDED WITH {self.remote}")
                    return
                if t == codec.END:
                    self._end_session(False, True, f"STATUS ARQ CONNECTION ENDED WITH {self.remote}")
                return

    def _become_irs(self) -> None:
        self._set_state("IRS")
        self.sub = "IRSfromISS"
        self.last_to_host = -1
        self._mem_type, self._mem_blocks = -1, []

    def _last_quality(self, d: Decoded) -> int:
        return int(d.extra.get("quality", 80))

    def _answer_conreq(self, t: int, d: Decoded) -> None:
        caller, target = d.extra["caller"].upper(), d.extra["target"].upper()
        self.pending_session = codec.session_id(caller, target)
        bw = negotiate_bw(self.bandwidth, t)
        if bw is None:
            self._event(f"REJECTEDBW {caller}")
            self._reply(modulate.encode_control(codec.CONREJBW, self.pending_session))
            return
        self._event(f"TARGET {target}")
        pending = self.pending_session
        self._reset_connection()
        self.pending_session = pending
        self._clear_outq()
        self.remote, self.local, self._final_id = caller, target, target
        self.pending = True
        self.pending_timeout = self.now + PENDING_TIMEOUT_MS
        self._timeout_trip = self.now
        self._set_state("IRS")
        self.sub = "IRSConAck"
        self.session_bw = bw
        self.received_leader = d.extra.get("leader_ms", 0)
        self._reply(modulate.encode_conack(bw, self.received_leader, pending))
