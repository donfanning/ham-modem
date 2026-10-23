"""PACTOR-I ARQ: the half-duplex synchronous stop-and-wait ARQ link.

Cycle: 1.25 s (1.40 s long path). The ISS sends a packet (0.96 s), the IRS answers with a 12-bit CS (0.12 s),
and 0.17 s of idle fills the rest. The master (caller) owns the clock.

Speed: 200 bd (24-byte packet: 1 header + 20 data + 1 status + 2 CRC) or 100 bd (12-byte packet: 1 header +
8 data + 1 status + 2 CRC). Speed changes via CS4; memory ARQ accumulates soft samples across retransmissions.
"""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from . import code, fsk

log = logging.getLogger("hammodem.pactor")

CYCLE_SP = int(1.250 * fsk.FS)       # standard path
CYCLE_LP = int(1.400 * fsk.FS)       # long path
PKT_SAMPLES = int(0.960 * fsk.FS)    # 7680 samples at 8 kHz
CS_BITS = 12
CS_SAMPLES = CS_BITS * fsk.spb(100)  # always 100 bd

PKT_100 = 11                          # 8 data + status + 2 CRC (no header)
PKT_200 = 23                          # 20 data + status + 2 CRC (no header)
DATA_100 = 8
DATA_200 = 20

CALL_SAMPLES = (8 + 64) * fsk.spb(100) + 64 * fsk.spb(200)   # header+call at 100 + call at 200
RX_WINDOW = fsk.FS // 4              # ±250 ms search window for VoIP jitter
BURST_THR = 0.001
BURST_MIN = 80

RETRY_CALL = 20
RETRY_QSO = 64
RETRY_HISPEED = 4
RETRY_QRT = 4

_CS_MAP = {code.CS1: 1, code.CS2: 2, code.CS3: 3, code.CS4: 4}


class MemoryArq:
    """Accumulate soft discriminator values across retransmissions of the same packet.

    When the hard decision fails CRC, ``selective_fec`` tries flipping the weakest
    bits (up to ``max_flip`` at a time, up to ``max_trials`` combinations) — a
    brute-force soft-decision FEC using the CRC as the error detector.
    """

    MAX_FLIP = 3
    MAX_TRIALS = 120

    def __init__(self, nbits: int) -> None:
        self.acc = np.zeros(nbits, float)
        self.count = 0

    def add(self, soft: list[float]) -> None:
        n = min(len(soft), len(self.acc))
        self.acc[:n] += np.array(soft[:n])
        self.count += 1

    def decide(self) -> list[int]:
        return [1 if v > 0 else 0 for v in self.acc]

    def selective_fec(self, pkt_bytes: int) -> bytes | None:
        """Try hard decision, then flip weakest bits until CRC passes or budget exhausted."""
        nbits = pkt_bytes * 8
        if len(self.acc) < nbits:
            return None
        bits = self.decide()[:nbits]
        raw = self._bits_to_bytes(bits, pkt_bytes)
        if code.crc_check(raw):
            return raw
        confidence = np.abs(self.acc[:nbits])
        weak = np.argsort(confidence)
        trials = 0
        for nf in range(1, min(self.MAX_FLIP, len(weak)) + 1):
            for combo in _combinations(weak, nf, self.MAX_TRIALS - trials):
                trials += 1
                trial_bits = bits[:]
                for idx in combo:
                    trial_bits[idx] ^= 1
                raw = self._bits_to_bytes(trial_bits, pkt_bytes)
                if code.crc_check(raw):
                    return raw
                if trials >= self.MAX_TRIALS:
                    return None
        return None

    @staticmethod
    def _bits_to_bytes(bits: list[int], nbytes: int) -> bytes:
        raw = bytearray(nbytes)
        for byte_i in range(nbytes):
            val = 0
            for bit_i in range(8):
                val |= bits[byte_i * 8 + bit_i] << bit_i
            raw[byte_i] = val
        return bytes(raw)

    def reset(self) -> None:
        self.acc[:] = 0
        self.count = 0


def _combinations(indices: np.ndarray, n: int, max_count: int):
    """Yield up to ``max_count`` n-element combinations from ``indices``."""
    from itertools import combinations as _comb
    count = 0
    for c in _comb(indices[:min(len(indices), n + 12)], n):
        yield c
        count += 1
        if count >= max_count:
            return


class PactorStation:
    """PACTOR-I ARQ station, following the AMTOR TNC pattern: process() runs from the audio clock."""

    def __init__(self, mycall: str, fs: int = fsk.FS, center: float = fsk.CENTER, shift: float = fsk.SHIFT,
                 level: int = fsk.LEVEL, longpath: bool = False,
                 on_text: Callable[[str], None] | None = None,
                 on_event: Callable[[str], None] | None = None) -> None:
        if fs != fsk.FS:
            raise ValueError("PACTOR-I runs at 8 kHz")
        self.mycall = mycall.upper()
        self.center, self.shift, self.level = center, shift, level
        self.longpath = longpath
        self.on_text, self.on_event = on_text, on_event
        self.mod = fsk.Modulator(center, shift, level)
        self.disc = fsk.Discriminator(center, shift, baud=200)
        self.disc100 = fsk.Discriminator(center, shift, baud=100)
        self.now = 0
        self._tx: list[tuple[int, np.ndarray]] = []
        self.state = "standby"          # standby | calling | iss | irs | ending
        self.master = False
        self.remote = ""
        self.baud = 200
        self.queue = bytearray()
        self.stats = {"pkts_sent": 0, "repeats": 0, "pkts_ok": 0, "pkts_bad": 0}
        self._reset_link()

    @property
    def cycle(self) -> int:
        return CYCLE_LP if self.longpath else CYCLE_SP

    # ----------------------------------------------------------------------- host API

    def connect(self, call: str) -> None:
        self._reset_link()
        self.remote = call.upper()
        self.master, self.state = True, "calling"
        self._call_n = 0
        self._next_cycle = self.now + fsk.FS // 10
        self._event(f"CALLING {self.remote}")

    def send(self, text: str) -> None:
        self.queue.extend(text.encode("latin-1", "replace"))

    def disconnect(self) -> None:
        if self.state == "iss":
            self._qrt_pending = True
        elif self.state == "irs":
            self._breakin_pending = True
            self._then_qrt = True

    @property
    def keyed(self) -> bool:
        return any(s + len(w) > self.now for s, w in self._tx)

    # ----------------------------------------------------------------------- audio

    def process(self, block: np.ndarray) -> np.ndarray:
        n = len(block)
        xf = np.asarray(block, dtype=float)
        d200, e200 = self.disc.process(xf)
        d100, e100 = self.disc100.process(xf)
        self._rx_disc200 = np.concatenate((self._rx_disc200, d200))
        self._rx_disc100 = np.concatenate((self._rx_disc100, d100))
        self._rx_energy = np.concatenate((self._rx_energy, e200))
        end = self.now + n
        while self.master and self.state not in ("standby", "irs") and self._next_cycle < end:
            self._master_cycle(self._next_cycle)
            self._next_cycle += self.cycle
        if self.master and self.state == "irs":
            self._master_irs()
        if not self.master:
            self._slave_check()
        out = np.zeros(n, np.int16)
        keep = []
        for s, w in self._tx:
            lo, hi = max(s, self.now), min(s + len(w), end)
            if hi > lo:
                out[lo - self.now:hi - self.now] = w[lo - s:hi - s]
            if s + len(w) > end:
                keep.append((s, w))
        self._tx = keep
        trim = max(0, len(self._rx_disc200) - self.cycle * 3)
        if trim > 0:
            self._rx_disc200 = self._rx_disc200[trim:]
            self._rx_disc100 = self._rx_disc100[trim:]
            self._rx_energy = self._rx_energy[trim:]
            self._rx_offset += trim
        self.now = end
        self._watchdog()
        return out

    # ----------------------------------------------------------------------- internals

    def _reset_link(self) -> None:
        self._next_cycle = 0
        self._counter = 0
        self._last_pkt_data = b""
        self._heard: str | int = "none"    # "none" | "bad" | 1 | 2 | 3 | 4
        self._cycles_quiet = 0
        self._call_n = 0
        self._hispeed_fails = 0
        self._breakin_pending = False
        self._then_qrt = False
        self._qrt_pending = False
        self._qrt_tries = 0
        self._handover = ""
        self._last_valid = self.now
        self._rx_disc200 = np.zeros(0)
        self._rx_disc100 = np.zeros(0)
        self._rx_energy = np.zeros(0)
        self._rx_offset = self.now
        self._marq = MemoryArq(PKT_200 * 8)
        self._cur_header = code.HEADER1
        self._exp_header = None
        self._irs_exp_counter = None
        self._scan_pos = self.now
        self._next_rx_est = 0

    def _event(self, text: str) -> None:
        if self.on_event:
            self.on_event(text)

    def _text(self, s: str) -> None:
        if s and self.on_text:
            self.on_text(s)

    def _to_standby(self, why: str) -> None:
        self.state = "standby"
        self.master = False
        self._event(f"STANDBY ({why})")

    # ----------------------------------------------------------------------- TX helpers

    def _tx_packet(self, start: int, data: bytes, hdr: int, baud: int, breakin: bool = False,
                   qrt: bool = False) -> None:
        huffman_ok = all(32 <= b < 128 for b in data)
        use_huffman = False
        pkt_data = data
        if huffman_ok and len(data) > 0:
            packed, nbits = code.huffman_encode(data)
            data_len = DATA_200 if baud == 200 else DATA_100
            if len(packed) <= data_len:
                use_huffman = True
                padded = bytearray(packed) + bytearray(data_len - len(packed))
                code.huffman_fill_idle(padded, nbits, data_len * 8)
                pkt_data = bytes(padded)
        if not use_huffman:
            data_len = DATA_200 if baud == 200 else DATA_100
            pkt_data = data[:data_len]
            pkt_data = pkt_data + bytes([code.IDLE] * (data_len - len(pkt_data)))
        pkt = code.build_packet(pkt_data, self._counter, huffman=use_huffman, breakin=breakin, qrt=qrt)
        m = fsk.Modulator(self.center, self.shift, self.level)
        hdr_bits = [(hdr >> i) & 1 for i in range(8)]
        pkt_bits = []
        for byte in pkt:
            for i in range(8):
                pkt_bits.append((byte >> i) & 1)
        audio = m.bits(hdr_bits + pkt_bits, baud)
        self._tx.append((max(start, self.now), audio))

    def _tx_cs(self, start: int, cs: int) -> None:
        m = fsk.Modulator(self.center, self.shift, self.level)
        audio = m.bits(code.cs_bits(cs), baud=100)
        self._tx.append((max(start, self.now), audio))

    def _tx_call(self, start: int) -> None:
        call_bytes = code.encode_call(self.remote)
        m = fsk.Modulator(self.center, self.shift, self.level)
        hdr_bits = [(code.HEADER1 >> i) & 1 for i in range(8)]
        call_100 = []
        for b in call_bytes:
            for i in range(8):
                call_100.append((b >> i) & 1)
        call_200 = []
        for b in call_bytes:
            for i in range(8):
                call_200.append((b >> i) & 1)
        audio_parts = [m.bits(hdr_bits, 100), m.bits(call_100, 100), m.bits(call_200, 200)]
        audio = np.concatenate(audio_parts)
        self._tx.append((max(start, self.now), audio))

    # ----------------------------------------------------------------------- RX helpers

    def _rx_cs_at(self, sample: int) -> int | None:
        """Try to decode a 12-bit CS at ``sample`` (relative to current buffer)."""
        idx = sample - self._rx_offset
        if idx < 0 or idx + CS_SAMPLES > len(self._rx_disc100):
            return None
        bits, _ = fsk.slice_bits(self._rx_disc100, idx, CS_BITS, baud=100)
        if len(bits) < CS_BITS:
            return None
        return code.cs_from_bits(bits)

    def _find_cs_window(self, center: int, window: int = RX_WINDOW) -> int | None:
        """Search for a CS in [center, center+window] by burst detection then decode."""
        lo = center - self._rx_offset
        hi = min(center + window - self._rx_offset, len(self._rx_energy))
        if hi <= lo or lo < 0:
            return None
        e = self._rx_energy[max(lo, 0):hi]
        start = fsk.detect_burst(e, threshold=BURST_THR, min_samples=min(BURST_MIN, CS_SAMPLES // 2))
        if start is None:
            return None
        abs_start = max(lo, 0) + self._rx_offset + start
        return self._rx_cs_at(abs_start)

    def _rx_packet_at(self, sample: int, baud: int) -> tuple[dict | None, list[float]]:
        """Try to decode a packet at ``sample``. Returns (parsed, soft_values)."""
        idx = sample - self._rx_offset
        disc = self._rx_disc200 if baud == 200 else self._rx_disc100
        pkt_bytes = PKT_200 if baud == 200 else PKT_100
        pkt_bits_n = pkt_bytes * 8
        hdr_bits_n = 8
        total_bits = hdr_bits_n + pkt_bits_n
        if idx < 0 or idx + total_bits * fsk.spb(baud) > len(disc):
            return None, []
        phase, _ = fsk.best_phase(disc, idx, total_bits, baud)
        all_bits, soft = fsk.slice_bits(disc, idx + phase, total_bits, baud)
        if len(all_bits) < total_bits:
            return None, soft
        hdr_val = sum(all_bits[i] << i for i in range(8))
        pkt_raw = bytearray()
        for byte_i in range(pkt_bytes):
            val = 0
            for bit_i in range(8):
                val |= all_bits[8 + byte_i * 8 + bit_i] << bit_i
            pkt_raw.append(val)
        parsed = code.parse_packet(bytes(pkt_raw))
        if parsed is not None:
            parsed["header"] = hdr_val
            parsed["baud"] = baud
        return parsed, soft[8:]  # soft values for the payload (no header)

    # ----------------------------------------------------------------------- master clock

    def _master_cycle(self, t: int) -> None:
        if self.state == "calling":
            self._cycles_quiet += 1
            if self._cycles_quiet > RETRY_CALL:
                self._to_standby("no answer")
                return
            # check for CS reply from PREVIOUS cycle before sending next call
            reply_start = t - self.cycle + PKT_SAMPLES
            cs = self._find_cs_window(reply_start)
            if cs is not None:
                self._cycles_quiet = 0
                cs_id = _CS_MAP.get(cs)
                if cs_id == 2:
                    self._to_standby("busy")
                    return
                if cs_id in (1, 4):
                    self.baud = 200 if cs_id == 1 else 100
                    self.state = "iss"
                    self._counter = 1
                    self._cur_header = code.HEADER2
                    self._event(f"LINKED {self.remote} at {self.baud} bd")
                    self.queue[:0] = self.mycall.encode("ascii") + b"\r"
                    return
            self._tx_call(t)
            self._call_n += 1
        elif self.state in ("iss", "ending"):
            heard, self._heard = self._heard, "none"
            reply_start = t - self.cycle + PKT_SAMPLES
            cs = self._find_cs_window(reply_start)
            if cs is not None:
                heard = _CS_MAP.get(cs, "bad")
            if heard in ("none", "bad"):
                self._cycles_quiet += 1
            else:
                self._cycles_quiet = 0
                self._last_valid = self.now
            if self._cycles_quiet > RETRY_QSO:
                self._to_standby("link lost")
                return
            self._iss_tx(t, heard)
        elif self.state == "irs":
            pass

    def _master_irs(self) -> None:
        """Master station in IRS: scan for packets like _slave_irs, runs every process() call."""
        if self._next_rx_est <= 0:
            return
        search_lo = max(self._next_rx_est - RX_WINDOW, self._rx_offset)
        search_hi = self._next_rx_est + self.cycle + RX_WINDOW
        avail = self.now - PKT_SAMPLES
        if avail < search_lo:
            return
        search_hi = min(search_hi, avail)
        burst = self._find_burst_in(search_lo, search_hi, min_samples=BURST_MIN)
        if burst is not None:
            pkt, soft = self._rx_packet_at(burst, self.baud)
            if pkt is not None:
                cs = self._irs_process(pkt, soft)
                self._tx_cs(burst + PKT_SAMPLES, cs)
                self._next_rx_est = burst + self.cycle
                self._cycles_quiet = 0
                self._last_valid = self.now
                return
        if self.now > self._next_rx_est + self.cycle + RX_WINDOW + PKT_SAMPLES:
            self._cycles_quiet += 1
            if self._cycles_quiet > RETRY_QSO:
                self._to_standby("link lost")
                return
            self._next_rx_est += self.cycle
            pkt, soft = None, []
            cs = self._irs_process(pkt, soft)
            self._tx_cs(self.now, cs)

    def _iss_tx(self, t: int, heard) -> None:
        if self.state == "ending":
            if self._qrt_tries > RETRY_QRT:
                self._to_standby("ended")
                return
            if heard in (1, 2, 3, 4) and self._qrt_tries >= 1:
                self._to_standby("ended")
                return
            self._qrt_tries += 1
            data_len = DATA_200 if self.baud == 200 else DATA_100
            call_rev = code.encode_call(self.remote)[::-1][:data_len]
            call_rev = call_rev + bytes([code.CALL_PAD] * (data_len - len(call_rev)))
            self._tx_packet(t, call_rev, self._cur_header, self.baud, qrt=True)
            return
        if self._qrt_pending:
            self._qrt_pending = False
            self.state = "ending"
            self._qrt_tries = 0
            self._iss_tx(t, heard)
            return
        if heard == 3:
            self._become_irs()
            return
        if heard == 4:
            if self.baud == 200:
                self.baud = 100
                self._event("SPEED DOWN to 100 bd")
            else:
                self.baud = 200
                self._hispeed_fails = 0
                self._event("SPEED UP to 200 bd")
        if heard in (1, 2):
            self._counter = (self._counter + 1) & 3
            self._cur_header = code.HEADER2 if self._cur_header == code.HEADER1 else code.HEADER1
            self._marq.reset()
            self._take_and_send(t)
        elif heard in ("none", "bad"):
            if self._last_pkt_data:
                self.stats["repeats"] += 1
                self._tx_packet(t, self._last_pkt_data, self._cur_header, self.baud,
                                breakin=self._breakin_pending)
            else:
                self._take_and_send(t)
        else:
            self._take_and_send(t)

    def _take_and_send(self, t: int) -> None:
        data_len = DATA_200 if self.baud == 200 else DATA_100
        data = bytes(self.queue[:data_len])
        del self.queue[:data_len]
        if not data:
            if self._then_qrt:
                self._then_qrt = False
                self._qrt_pending = True
                return
            data = bytes([code.IDLE] * data_len)
        self._last_pkt_data = data
        self.stats["pkts_sent"] += 1
        self._tx_packet(t, data, self._cur_header, self.baud, breakin=self._breakin_pending)
        self._breakin_pending = False

    # ----------------------------------------------------------------------- IRS

    def _irs_process(self, pkt: dict | None, soft: list[float]) -> int:
        if pkt is None:
            self._marq.add(soft) if soft else None
            self.stats["pkts_bad"] += 1
            # try memory ARQ with selective FEC (flip weakest bits if CRC fails)
            if self._marq.count >= 2:
                pkt_bytes = PKT_200 if self.baud == 200 else PKT_100
                raw = self._marq.selective_fec(pkt_bytes)
                if raw is not None:
                    pkt = code.parse_packet(raw)
                    if pkt is not None:
                        pkt["baud"] = self.baud
                        pkt["header"] = 0
                        log.debug("memory ARQ recovered packet after %d tries", self._marq.count)
            if pkt is None:
                return code.CS1 if (self._irs_exp_counter or 0) % 2 == 0 else code.CS2
        if pkt is not None:
            self.stats["pkts_ok"] += 1
            self._marq.reset()
            self._last_valid = self.now
            if pkt.get("qrt"):
                self._to_standby("ended by remote")
                return code.CS1
            if pkt.get("breakin"):
                self._become_iss_from_irs()
                return code.CS3
            cs = code.CS1 if pkt["counter"] % 2 == 0 else code.CS2
            is_dup = (self._irs_exp_counter is not None
                      and pkt["counter"] == (self._irs_exp_counter - 1) & 3)
            if not is_dup:
                data = pkt["data"]
                if pkt.get("huffman"):
                    max_chars = DATA_200 if self.baud == 200 else DATA_100
                    data = code.huffman_decode(data, len(data) * 8)[:max_chars]
                text = ""
                for b in data:
                    if b == code.IDLE:
                        continue
                    if 32 <= b < 127:
                        text += chr(b)
                    elif b == 0x0D:
                        text += "\r\n"
                    elif b == 0x0A:
                        text += "\n"
                self._text(text)
                self._irs_exp_counter = (pkt["counter"] + 1) & 3
            if self._breakin_pending or self.queue:
                self._breakin_pending = False
                self._become_iss_from_irs()
                return code.CS3
            if self.baud == 200 and self.stats["pkts_bad"] > self.stats["pkts_ok"] * 2:
                return code.CS4
            return cs
        return code.CS1

    # ----------------------------------------------------------------------- changeover

    def _become_irs(self) -> None:
        self.state = "irs"
        self._irs_exp_counter = None
        self._marq = MemoryArq((PKT_200 if self.baud == 200 else PKT_100) * 8)
        self._cycles_quiet = 0
        if self.master:
            self._next_rx_est = self._next_cycle
        else:
            self._next_rx_est = self.now + self.cycle
            self._scan_pos = self.now
        self._event("CHANGEOVER to IRS")

    def _become_iss_from_irs(self) -> None:
        self.state = "iss"
        self._counter = 0
        self._last_pkt_data = b""
        self._cur_header = code.HEADER1
        self._next_cycle = self.now + self.cycle
        self._event("CHANGEOVER to ISS")

    # ----------------------------------------------------------------------- slave RX

    def _rx_call_at(self, sample: int) -> str | None:
        """Try to decode a call at ``sample``: header (8 bits, 100 bd) + callsign (64 bits, 100 bd)."""
        idx = sample - self._rx_offset
        spb100 = fsk.spb(100)
        need = 72 * spb100
        if idx < 0 or idx + need > len(self._rx_disc100):
            return None
        phase, score = fsk.best_phase(self._rx_disc100, idx, 72, baud=100)
        if score < 0:
            return None
        bits, _ = fsk.slice_bits(self._rx_disc100, idx + phase, 72, baud=100)
        if len(bits) < 72:
            return None
        hdr = sum(bits[i] << i for i in range(8))
        if hdr != code.HEADER1:
            return None
        call_raw = bytearray()
        for byte_i in range(8):
            val = 0
            for bit_i in range(8):
                val |= bits[8 + byte_i * 8 + bit_i] << bit_i
            call_raw.append(val)
        return code.decode_call(bytes(call_raw))

    def _find_burst_in(self, t_min: int, t_max: int, min_samples: int = BURST_MIN) -> int | None:
        """Find the start of an energy burst in [t_min, t_max)."""
        lo = max(t_min - self._rx_offset, 0)
        hi = min(t_max - self._rx_offset, len(self._rx_energy))
        if hi - lo < min_samples:
            return None
        e = self._rx_energy[lo:hi]
        start = fsk.detect_burst(e, threshold=BURST_THR, min_samples=min_samples)
        if start is None:
            return None
        return lo + self._rx_offset + start

    def _slave_check(self) -> None:
        """Non-master station: detect incoming transmissions and respond."""
        if self.master:
            return
        if self.state == "standby":
            self._slave_standby()
        elif self.state == "irs":
            self._slave_irs()
        elif self.state in ("iss", "ending"):
            self._slave_iss()

    def _slave_standby(self) -> None:
        scan_end = self.now - CALL_SAMPLES
        if scan_end <= self._scan_pos:
            return
        burst = self._find_burst_in(self._scan_pos, scan_end)
        if burst is None:
            self._scan_pos = max(self._scan_pos, scan_end - fsk.FS)
            return
        # try to decode as a call
        call = self._rx_call_at(burst)
        if call is not None and call == self.mycall:
            self._reset_link()
            self.master = False
            self.state = "irs"
            self._last_valid = self.now
            self._next_rx_est = burst + self.cycle
            reply_at = burst + CALL_SAMPLES
            self._tx_cs(reply_at, code.CS1)
            self._marq = MemoryArq(PKT_200 * 8)
            self._event(f"CALLED by ?")
            self._scan_pos = burst + CALL_SAMPLES + CS_SAMPLES
            return
        # try to decode as FEC packet
        for baud in (200, 100):
            pkt, _ = self._rx_packet_at(burst, baud)
            if pkt is not None:
                data = pkt["data"]
                if pkt.get("huffman"):
                    data = code.huffman_decode(data, len(data) * 8)
                text = ""
                for b in data:
                    if b == code.IDLE:
                        continue
                    if 32 <= b < 127:
                        text += chr(b)
                    elif b == 0x0D:
                        text += "\r\n"
                    elif b == 0x0A:
                        text += "\n"
                self._text(text)
                break
        self._scan_pos = burst + fsk.FS // 2

    def _slave_irs(self) -> None:
        if self._next_rx_est <= 0:
            return
        search_lo = max(self._scan_pos, self._next_rx_est - RX_WINDOW)
        search_hi = self._next_rx_est + self.cycle + RX_WINDOW
        avail = self.now - PKT_SAMPLES
        if avail < search_lo:
            return
        search_hi = min(search_hi, avail)
        pos = search_lo
        while pos < search_hi:
            burst = self._find_burst_in(pos, search_hi, min_samples=BURST_MIN)
            if burst is None:
                break
            pkt, soft = self._rx_packet_at(burst, self.baud)
            if pkt is not None:
                cs = self._irs_process(pkt, soft)
                self._tx_cs(burst + PKT_SAMPLES, cs)
                self._next_rx_est = burst + self.cycle
                self._scan_pos = burst + PKT_SAMPLES
                self._cycles_quiet = 0
                self._last_valid = self.now
                return
            pos = burst + PKT_SAMPLES
        if self.now > self._next_rx_est + self.cycle + RX_WINDOW:
            self._cycles_quiet += 1
            if self._cycles_quiet > RETRY_QSO:
                self._to_standby("link lost")
                return
            self._next_rx_est += self.cycle

    def _slave_iss(self) -> None:
        if self._next_cycle <= 0:
            return
        end = self.now
        while self._next_cycle < end:
            heard, self._heard = self._heard, "none"
            cs = self._find_cs_window(self._next_cycle - self.cycle + PKT_SAMPLES)
            if cs is not None:
                heard = _CS_MAP.get(cs, "bad")
            if heard in ("none", "bad"):
                self._cycles_quiet += 1
            else:
                self._cycles_quiet = 0
                self._last_valid = self.now
            if self._cycles_quiet > RETRY_QSO:
                self._to_standby("link lost")
                return
            self._iss_tx(self._next_cycle, heard)
            self._next_cycle += self.cycle

    # ----------------------------------------------------------------------- watchdog

    def _watchdog(self) -> None:
        if self.state in ("iss", "irs") and self.now - self._last_valid > RETRY_QSO * self.cycle:
            self._to_standby("link lost")
