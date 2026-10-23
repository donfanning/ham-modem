"""G3RUH / K9NG baseband FSK at 9600, 19200 or 38400 bd.

Line coding, as Direwolf does it (needed for interop): HDLC -> NRZI -> self-synchronising scrambler
1 + x^12 + x^17 -> +/-1 levels -> raised-cosine low-pass -> straight into the FM modulator (here: the audio leg).
Polarity does not matter: an inverted signal descrambles to inverted NRZI, which decodes the same.

The signal occupies 0 to (1 + beta) x baud / 2, so it needs a linear leg end to end: 9600 bd fits L16 at 16 kHz,
19200 bd needs 32 kHz or more, 38400 bd needs 48 kHz (with the roll-off squeezed to beta = 0.2). G.711's 4 kHz
Nyquist limit carries none of them, and the modem refuses a rate that cannot hold its band.

DSP runs at an internal 5 samples per symbol (48 / 96 / 192 kHz): transmit shapes there and decimates to the leg
rate; receive interpolates up, low-passes, removes DC, then slices and recovers the clock with the same PLL scheme
as the AFSK receiver.
"""

from __future__ import annotations

import math

import numpy as np

from .afsk import _Fir, lowpass_taps
from ..fx25 import Fx25Receiver
from ..fx25 import burst_bits as fx25_burst_bits
from ..il2p import Il2pReceiver
from ..il2p import burst_levels as il2p_burst_levels
from .hdlc import DEDUPE_NEAR_BITS, HdlcDecoder, NrziDecoder, NrziEncoder, extend_bits, span_mask, track_burst

ERASE_TAIL = 18                         # NRZI (1) + descrambler taps (17): how far one bad line bit reaches

SPS = 5                                 # internal samples per symbol
BAUDS = (9600, 19200, 38400)
MIN_BETA = 0.15


def plan(baud: int, fs: int) -> tuple[int, int, float]:
    """(internal rate, fs multiple, widest usable roll-off) for ``baud`` on an ``fs`` leg, or ValueError."""
    if baud not in BAUDS:
        raise ValueError(f"G3RUH baud must be one of {BAUDS}")
    internal = baud * SPS
    beta = min(0.5, 0.96 * fs / baud - 1)       # keep the band under 96% of Nyquist
    if internal % fs or beta < MIN_BETA:
        ok = [r for r in (16000, 24000, 32000, 48000, 96000) if (baud * SPS) % r == 0 and 0.96 * r / baud - 1 >= MIN_BETA]
        raise ValueError(f"G3RUH {baud} bd cannot run on a {fs} Hz leg; use {' or '.join(map(str, ok))} Hz "
                         f"linear audio (G.711 cannot carry baseband G3RUH)")
    return internal, internal // fs, beta


def raised_cosine(beta: float, span: int, sps: int) -> np.ndarray:
    t = (np.arange(span * sps + 1) - span * sps / 2) / sps
    h = np.sinc(t)
    denom = 1 - (2 * beta * t) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        h *= np.where(np.abs(denom) < 1e-8, np.pi / 4, np.cos(np.pi * beta * t) / denom)
    return h


def _dedupe(dem, frames: list[bytes], nbits: int) -> list[bytes]:
    """Report a frame once per burst (see hdlc.DEDUPE_NEAR_BITS); retransmissions in a new burst get through."""
    out = []
    burst = getattr(dem, "_burst", 0)
    for f in frames:
        seen = dem._recent.get(f)
        if seen is None or (seen[1] != burst and dem._bits_seen - seen[0] > DEDUPE_NEAR_BITS):
            out.append(f)
            dem.frames += 1
        dem._recent[f] = (dem._bits_seen, burst)
    dem._bits_seen += nbits
    if len(dem._recent) > 64:
        dem._recent = {k: v for k, v in dem._recent.items() if v[1] >= burst - 1}
    return out


class Scrambler:
    """Direwolf's ``scramble``: out = in ^ s[16] ^ s[11], output shifted into the state."""

    def __init__(self) -> None:
        self.state = 0

    def scramble(self, bits: list[int]) -> list[int]:
        s, out = self.state, []
        for b in bits:
            o = (b ^ (s >> 16) ^ (s >> 11)) & 1
            s = ((s << 1) | o) & 0x1FFFF
            out.append(o)
        self.state = s
        return out


class G3ruhModulator:
    def __init__(self, fs: int = 16000, baud: int = 9600, amplitude: float = 0.5) -> None:
        self.fs, self.baud, self.amplitude = fs, baud, amplitude
        _, self.decim, beta = plan(baud, fs)
        self.nrzi, self.scr = NrziEncoder(), Scrambler()
        self.fx25 = 0                      # FX.25 check bytes per frame (16/32/64), 0 = plain AX.25
        self.il2p: bool | None = None      # send IL2P instead (True: max FEC, False: normal FEC)
        self._shape = _Fir(raised_cosine(beta, 8, SPS))
        self._phase = 0                    # decimation phase carried across bursts

    def flags_for(self, seconds: float) -> int:
        return max(1, math.ceil(seconds * self.baud / 8))

    def modulate_bits(self, bits: list[int]) -> np.ndarray:
        return self.modulate_levels(self.scr.scramble(self.nrzi.encode(bits)))

    def modulate_levels(self, line: list[int]) -> np.ndarray:
        """Shape line levels directly -- IL2P bypasses NRZI and the G3RUH scrambler."""
        levels = np.array(line, dtype=np.float64) * 2 - 1
        x = np.zeros(len(levels) * SPS)
        x[::SPS] = levels
        y = self._shape(x)
        start = (-self._phase) % self.decim
        out = y[start::self.decim]
        self._phase = (self._phase + len(y)) % self.decim
        return np.round(np.clip(out * self.amplitude, -1, 1) * 32767).astype(np.int16)

    def modulate(self, frames: list[bytes], txdelay: float = 0.1, txtail: float = 0.01) -> np.ndarray:
        if self.il2p is not None:
            return self.modulate_levels(il2p_burst_levels(frames, self.flags_for(txdelay),
                                                          max(3, self.flags_for(txtail)), self.il2p))
        # A few trailing flags flush the shaping filter and let the far scrambler/PLL finish the last frame.
        return self.modulate_bits(fx25_burst_bits(frames, self.flags_for(txdelay), max(3, self.flags_for(txtail)),
                                                  self.fx25))


class G3ruhDemodulator:
    INERTIA_LOCKED = 0.74
    INERTIA_SEARCH = 0.50
    DCD_THRESHOLD = 0.4

    def __init__(self, fs: int = 16000, baud: int = 9600, energy_dcd_dbfs: float | None = -45.0) -> None:
        self.fs, self.baud = fs, baud
        internal, self.interp, beta = plan(baud, fs)
        # One filter does both jobs: interpolation image rejection and the receive low-pass. Input already at
        # 5 samples/symbol decodes best with a wide, short filter (Direwolf's 9600 gen_packets ramp: 53 -> 60 of
        # Direwolf's 65); interpolated input needs the narrower, longer one to keep images and in-band noise down.
        # A squeezed roll-off (38400 bd on 48 kHz, beta 0.2) puts the band edge next to the first image, so the
        # filter needs a sharper transition: 16 symbols of taps (40/40 clean vs 39/40 with 8).
        # 19200 bd from 48 kHz (x2) behaves like native input (Direwolf's 19200 ramp: 28 -> 38 of its 43).
        cutoff, span = (0.8, 4) if self.interp <= 2 else (0.6, 16 if beta < 0.3 else 8)
        cutoff_hz = min(cutoff * baud, 0.48 * fs)
        self._lp = _Fir(lowpass_taps(cutoff_hz, internal, span * SPS * 2 + 1) * self.interp)
        self._dc_win = 64 * SPS
        self._dc_hist = np.zeros(self._dc_win)
        self.step = 2.0 / SPS
        self.phase = 0.0
        self.prev = 0
        self.quality = 0.0
        self.run = 0
        self.desc_state = 0
        self.nrzi = NrziDecoder()
        self.hdlc = HdlcDecoder()
        self.fx25 = Fx25Receiver()
        self.il2p = Il2pReceiver()
        self._recent: dict[bytes, int] = {}
        self._bits_seen = 0
        self._abs = 0                       # internal samples processed
        self._spans: list[tuple[int, int]] = []
        self._erase_delay = len(self._lp.taps) - 1
        self._since = 1 << 30
        self.energy_dcd_dbfs = energy_dcd_dbfs
        self.energy_busy = False
        self.frames = 0

    @property
    def dcd(self) -> bool:
        return self.energy_busy or self.quality > self.DCD_THRESHOLD

    @property
    def bad_fcs(self) -> int:
        return self.hdlc.bad_fcs

    def feed(self, pcm: np.ndarray | bytes, erased: bool = False) -> list[bytes]:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            pcm = np.frombuffer(pcm, dtype="<i2")
        n_int = len(pcm) * self.interp
        track_burst(self)
        if erased:
            self._spans.append((self._abs, self._abs + n_int + self._erase_delay))
        self._spans = [sp for sp in self._spans if sp[1] > self._abs]
        self._mask = span_mask(self._spans, self._abs, n_int).tolist() if self._spans else None
        self._abs += n_int
        x = np.asarray(pcm, dtype=np.float64) / 32768.0
        if self.energy_dcd_dbfs is not None and len(x):
            self.energy_busy = 10 * np.log10(np.mean(x * x) * 2 + 1e-12) > self.energy_dcd_dbfs
        up = np.zeros(len(x) * self.interp)
        up[::self.interp] = x
        y = self._lp(up)
        # Running-mean DC removal (radios add offset; a VoIP leg normally has none).
        buf = np.concatenate((self._dc_hist, y))
        c = np.cumsum(buf)
        mean = (c[self._dc_win:] - c[:-self._dc_win]) / self._dc_win
        self._dc_hist = buf[-self._dc_win:]
        return self._run((y - mean[-len(y):] > 0).tolist())

    @property
    def fx25_corrected(self) -> int:
        return self.fx25.corrected

    def _run(self, levels: list[bool]) -> list[bytes]:
        """Demodulate internal-rate samples to bits.

        Phase crossing detection is vectorised: for each constant-level segment between transitions,
        crossing positions are found analytically (O(1) numpy per segment) instead of iterating
        every sample.  Per-bit work (descrambler, NRZI, HDLC) runs at the symbol rate.
        """
        arr = np.asarray(levels, np.int8)
        n = len(arr)
        if n == 0:
            return []

        # Segment boundaries at transitions + end sentinel.
        prev_arr = np.empty(n, np.int8)
        prev_arr[0] = self.prev
        prev_arr[1:] = arr[:-1]
        trans = np.flatnonzero(arr != prev_arr)
        seg_ends = np.concatenate([trans, [n]])

        step = self.step
        phase = self.phase
        prev = int(self.prev)
        quality = self.quality
        run = self.run
        ds = self.desc_state
        nrzi, hdlc = self.nrzi, self.hdlc
        mask = self._mask

        out: list[bytes] = []
        bits: list[int] = []
        raw: list[int] = []
        flags: list[bool] = []

        seg_start = 0
        for seg_end in seg_ends:
            level = int(arr[seg_start])

            if level != prev:                  # transition at seg_start
                quality = quality * 0.96 + (0.04 if abs(phase) < 0.35 else -0.04)
                phase *= self.INERTIA_LOCKED if hdlc.active else self.INERTIA_SEARCH
                prev = level
                run = 0

            L = int(seg_end) - seg_start
            # Analytical crossing detection: crossing k (0-indexed) in a segment of L samples
            # occurs at sample j_k = max(0, ceil((1+2k-phase)/step - 1)).
            # Maximum k in this segment: floor((L*step + phase - 1) / 2).
            max_k = int(np.floor((L * step + phase - 1.0) / 2))
            if max_k >= 0:
                k_arr = np.arange(max_k + 1)
                j_local = np.maximum(0, np.ceil((1.0 + 2 * k_arr - phase) / step - 1.0 - 1e-10)).astype(int)
                valid = j_local < L
                j_local = j_local[valid]
                k_last = int(k_arr[valid][-1]) if valid.any() else -1
                for jl in j_local:
                    run += 1
                    if run > 24:               # scrambled data never holds a level this long
                        quality = max(-1.0, quality - 0.1)
                    raw.append(level)
                    bit = (level ^ (ds >> 16) ^ (ds >> 11)) & 1
                    ds = ((ds << 1) | level) & 0x1FFFF
                    d = nrzi.decode(bit)
                    bits.append(d)
                    if mask is not None:
                        flags.append(mask[seg_start + int(jl)])
                    frame = hdlc.push(d)
                    if frame is not None:
                        out.append(frame)
                # Phase at end of segment (no per-sample loop needed).
                phase = phase + step * L - 2.0 * (k_last + 1)
            else:
                phase = phase + step * L

            seg_start = int(seg_end)
            if seg_start >= n:
                break

        self.prev, self.phase, self.quality, self.run, self.desc_state = (
            prev, phase, max(-1.0, min(1.0, quality)), run, ds)
        if flags:
            ext, self._since = extend_bits(np.array(flags), self._since, ERASE_TAIL)
        else:
            ext, self._since = None, self._since + len(bits)
        out += self.fx25.feed_bits(bits, ext)
        out += self.il2p.feed_bits(raw)
        return _dedupe(self, out, len(bits))
