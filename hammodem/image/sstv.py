"""SSTV (Slow-Scan Television) modem.

Implements the standard VIS header and five common modes:
    Robot 36   (VIS=8,  320×240 YCrCb, 36 s)
    Martin M1  (VIS=44, 320×256 RGB,  114 s)
    Martin M2  (VIS=40, 320×256 RGB,   58 s)
    Scottie S1 (VIS=60, 320×256 RGB,  110 s)
    Scottie S2 (VIS=56, 320×256 RGB,   71 s)

All tones are FM audio (instantaneous frequency).
    1200 Hz — horizontal sync / VIS bits (logic 1)
    1300 Hz — VIS bit 0 (logic 0)
    1500 Hz — black
    2300 Hz — white

Images are represented as numpy uint8 arrays:
    Color modes: H×W×3  (RGB channel order)
    Robot 36:   H×W×3  (RGB, internally converted to YCrCb)
"""

import numpy as np
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FS = 8000

FREQ_SYNC  = 1200.0   # horizontal sync and VIS leader / bit-1
FREQ_VIS0  = 1300.0   # VIS bit 0
FREQ_BLACK = 1500.0   # black level
FREQ_WHITE = 2300.0   # white level
FREQ_RANGE = FREQ_WHITE - FREQ_BLACK   # 800 Hz

# VIS codes (standard values)
VIS_ROBOT36   =  8
VIS_MARTIN_M2 = 40
VIS_MARTIN_M1 = 44
VIS_SCOTTIE_S2 = 56
VIS_SCOTTIE_S1 = 60

# ---------------------------------------------------------------------------
# FM helpers
# ---------------------------------------------------------------------------

def _tone(freq: float, duration_s: float, fs: int = FS,
          init_phase: float = 0.0) -> tuple[np.ndarray, float]:
    n = max(1, int(round(duration_s * fs)))
    freqs = np.full(n, freq)
    phase = init_phase + np.cumsum(2.0 * np.pi * freqs / fs)
    return np.sin(phase), float(phase[-1])


def _scan(pixels: np.ndarray, duration_s: float, fs: int = FS,
          init_phase: float = 0.0) -> tuple[np.ndarray, float]:
    """Map a 1-D uint8 pixel array to FM audio spanning `duration_s` seconds."""
    n = max(1, int(round(duration_s * fs)))
    src = np.linspace(0, len(pixels) - 1, n)
    px  = np.interp(src, np.arange(len(pixels)), pixels.astype(np.float64))
    freqs = FREQ_BLACK + (px / 255.0) * FREQ_RANGE
    phase = init_phase + np.cumsum(2.0 * np.pi * freqs / fs)
    return np.sin(phase), float(phase[-1])


def _concat_segs(segs: list[np.ndarray]) -> np.ndarray:
    return np.concatenate(segs) if segs else np.array([], dtype=np.float64)


# ---------------------------------------------------------------------------
# VIS header encode / decode
# ---------------------------------------------------------------------------

def _vis_header(vis_code: int, fs: int = FS) -> tuple[np.ndarray, float]:
    """Encode a standard 8-bit VIS header.

    Structure (standard SSTV VIS):
        1900 Hz 300 ms leader
        1200 Hz  10 ms break
        1900 Hz 300 ms leader
        1200 Hz  30 ms start bit (logic 1)
        bits 0-6: 30 ms each (1300 Hz=0, 1200 Hz=1)
        parity  : 30 ms
        1200 Hz  30 ms stop bit
    """
    segs: list[np.ndarray] = []
    phase = 0.0

    seg, phase = _tone(1900.0, 0.300, fs, phase)
    segs.append(seg)
    seg, phase = _tone(FREQ_SYNC, 0.010, fs, phase)
    segs.append(seg)
    seg, phase = _tone(1900.0, 0.300, fs, phase)
    segs.append(seg)
    seg, phase = _tone(FREQ_SYNC, 0.030, fs, phase)
    segs.append(seg)

    parity = 0
    for bit_idx in range(7):
        b = (vis_code >> bit_idx) & 1
        parity ^= b
        freq = FREQ_VIS0 if b == 0 else FREQ_SYNC
        seg, phase = _tone(freq, 0.030, fs, phase)
        segs.append(seg)

    # even parity
    parity_freq = FREQ_VIS0 if parity == 0 else FREQ_SYNC
    seg, phase = _tone(parity_freq, 0.030, fs, phase)
    segs.append(seg)
    seg, phase = _tone(FREQ_SYNC, 0.030, fs, phase)
    segs.append(seg)

    return _concat_segs(segs), phase


def _decode_vis(freq: np.ndarray, fs: int = FS) -> Optional[int]:
    """Scan a frequency track for a VIS header; return VIS code or None."""
    n = len(freq)
    # Window sizes in samples
    leader_n = int(round(0.300 * fs))
    brk_n    = int(round(0.010 * fs))
    bit_n    = int(round(0.030 * fs))

    tol_sync  = 60.0
    tol_vis   = 60.0
    tol_lead  = 60.0

    # Scan for leader: 1900 Hz region
    i = 0
    while i + leader_n < n:
        window = freq[i:i + leader_n]
        if np.mean(np.abs(window - 1900.0)) < tol_lead:
            # Found first leader; skip break and second leader
            pos = i + leader_n + brk_n + leader_n
            # Verify start bit at ~1200 Hz
            if pos + bit_n > n:
                i += 1
                continue
            start_w = freq[pos:pos + bit_n]
            if np.mean(np.abs(start_w - FREQ_SYNC)) > tol_sync * 2:
                i += 1
                continue
            pos += bit_n
            # Read 7 data bits + parity + stop
            bits = []
            for _ in range(8):
                if pos + bit_n > n:
                    break
                bit_w = freq[pos:pos + bit_n]
                f_mean = np.mean(bit_w)
                bits.append(0 if abs(f_mean - FREQ_VIS0) < abs(f_mean - FREQ_SYNC) else 1)
                pos += bit_n
            if len(bits) < 8:
                i += 1
                continue
            vis = sum(bits[b] << b for b in range(7))
            return vis
        i += 1
    return None


# ---------------------------------------------------------------------------
# RGB ↔ YCrCb helpers (Robot 36 uses Y-Cr-Cb on alternating lines)
# ---------------------------------------------------------------------------

def _rgb_to_ycrcb(rgb: np.ndarray) -> np.ndarray:
    """Convert H×W×3 uint8 RGB → H×W×3 float64 YCrCb in [0,255]."""
    r = rgb[:, :, 0].astype(np.float64)
    g = rgb[:, :, 1].astype(np.float64)
    b = rgb[:, :, 2].astype(np.float64)
    y  = 16.0  + 65.481 * r / 255.0 + 128.553 * g / 255.0 + 24.966 * b / 255.0
    cb = 128.0 - 37.797 * r / 255.0 -  74.203 * g / 255.0 + 112.000 * b / 255.0
    cr = 128.0 + 112.000 * r / 255.0 - 93.786 * g / 255.0 - 18.214 * b / 255.0
    return np.stack([y, cr, cb], axis=2)


def _ycrcb_to_rgb(ycrcb: np.ndarray) -> np.ndarray:
    """Convert H×W×3 float64 YCrCb [0,255] → H×W×3 uint8 RGB."""
    y  = ycrcb[:, :, 0]
    cr = ycrcb[:, :, 1]
    cb = ycrcb[:, :, 2]
    y16  = y  - 16.0
    cr128 = cr - 128.0
    cb128 = cb - 128.0
    r = 255.0 / 219.0 * y16 + 255.0 / 112.0 * 0.701 * cr128
    g = 255.0 / 219.0 * y16 - 255.0 / 112.0 * 0.886 * 0.114 / 0.587 * cb128 \
                             - 255.0 / 112.0 * 0.701 * 0.299 / 0.587 * cr128
    b = 255.0 / 219.0 * y16 + 255.0 / 112.0 * 0.886 * cb128
    rgb = np.stack([r, g, b], axis=2)
    return np.clip(rgb, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Mode descriptors
# ---------------------------------------------------------------------------

class _SstvMode:
    """Base class; subclasses implement encode_image / decode_lines."""

    vis_code: int = 0
    width:    int = 320
    height:   int = 256

    def encode(self, image: np.ndarray, fs: int = FS) -> np.ndarray:
        raise NotImplementedError

    def decode(self, freq: np.ndarray, fs: int = FS) -> Optional[np.ndarray]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Robot 36
# ---------------------------------------------------------------------------

class _Robot36(_SstvMode):
    """Robot 36 — YCrCb, 320×240, 36 s.

    Line timing (ms): 9 sync + 3 separator + 88 Y + 4.5 separator + 44 Cr or Cb.
    Cr on even lines, Cb on odd lines.
    """

    vis_code = VIS_ROBOT36
    width    = 320
    height   = 240

    _SYNC_S = 0.009
    _SEP_S  = 0.003
    _SEP2_S = 0.0045
    _Y_S    = 0.088
    _C_S    = 0.044

    def encode(self, image: np.ndarray, fs: int = FS) -> np.ndarray:
        if image.shape != (self.height, self.width, 3):
            image = _resize_image(image, self.height, self.width)
        ycrcb = _rgb_to_ycrcb(image)
        y_ch  = np.clip(ycrcb[:, :, 0], 0, 255).astype(np.uint8)
        cr_ch = np.clip(ycrcb[:, :, 1], 0, 255).astype(np.uint8)
        cb_ch = np.clip(ycrcb[:, :, 2], 0, 255).astype(np.uint8)

        segs: list[np.ndarray] = []
        phase = 0.0
        header, phase = _vis_header(self.vis_code, fs)
        segs.append(header)

        for row in range(self.height):
            c_ch = cr_ch[row] if row % 2 == 0 else cb_ch[row]
            seg, phase = _tone(FREQ_SYNC, self._SYNC_S, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_BLACK, self._SEP_S, fs, phase)
            segs.append(seg)
            seg, phase = _scan(y_ch[row], self._Y_S, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_BLACK, self._SEP2_S, fs, phase)
            segs.append(seg)
            seg, phase = _scan(c_ch, self._C_S, fs, phase)
            segs.append(seg)

        audio = _concat_segs(segs)
        return np.clip(audio * 32767, -32768, 32767).astype(np.int16)

    def decode(self, freq: np.ndarray, fs: int = FS) -> Optional[np.ndarray]:
        line_s = self._SYNC_S + self._SEP_S + self._Y_S + self._SEP2_S + self._C_S
        line_n = int(round(line_s * fs))
        starts = _find_sync_lines(freq, int(round(self._SYNC_S * fs)), fs, line_n,
                                  offset=_VIS_HEADER_N)
        if not starts:
            return None

        y_arr  = np.zeros((self.height, self.width), dtype=np.uint8)
        cr_arr = np.zeros((self.height, self.width), dtype=np.uint8)
        cb_arr = np.zeros((self.height, self.width), dtype=np.uint8)

        sync_n  = int(round(self._SYNC_S * fs))
        sep_n   = int(round(self._SEP_S * fs))
        y_n     = int(round(self._Y_S * fs))
        sep2_n  = int(round(self._SEP2_S * fs))

        for idx, start in enumerate(starts[:self.height]):
            if start + line_n > len(freq):
                break
            off = start + sync_n + sep_n
            y_pix  = _freqs_to_pixels(freq[off:off + y_n], self.width)
            off   += y_n + sep2_n
            c_pix  = _freqs_to_pixels(freq[off:off + int(round(self._C_S * fs))], self.width)
            y_arr[idx]  = y_pix
            if idx % 2 == 0:
                cr_arr[idx] = c_pix
            else:
                cb_arr[idx] = c_pix

        # Fill Cr/Cb for every line (copy from neighbour)
        for row in range(0, self.height - 1, 2):
            cr_arr[row + 1] = cr_arr[row]
        for row in range(1, self.height, 2):
            if row + 1 < self.height:
                cb_arr[row - 1] = cb_arr[row]

        ycrcb = np.stack([y_arr.astype(np.float64),
                          cr_arr.astype(np.float64),
                          cb_arr.astype(np.float64)], axis=2)
        return _ycrcb_to_rgb(ycrcb)


# ---------------------------------------------------------------------------
# Martin M1 / M2
# ---------------------------------------------------------------------------

class _MartinBase(_SstvMode):
    """Martin M1 and M2 — RGB, 320×256.

    Line order per row: G sync B R (with horizontal sync between each channel).
    """
    width    = 320
    height   = 256

    _SYNC_S: float = 0.0
    _SCAN_G: float = 0.0
    _SCAN_B: float = 0.0
    _SCAN_R: float = 0.0
    _PORCH_S: float = 0.000572

    def encode(self, image: np.ndarray, fs: int = FS) -> np.ndarray:
        if image.shape != (self.height, self.width, 3):
            image = _resize_image(image, self.height, self.width)
        r = image[:, :, 0]
        g = image[:, :, 1]
        b = image[:, :, 2]

        segs: list[np.ndarray] = []
        phase = 0.0
        header, phase = _vis_header(self.vis_code, fs)
        segs.append(header)

        for row in range(self.height):
            # Sync pulse
            seg, phase = _tone(FREQ_SYNC, self._SYNC_S, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_BLACK, self._PORCH_S, fs, phase)
            segs.append(seg)
            # G B R channels
            seg, phase = _scan(g[row], self._SCAN_G, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_SYNC, self._SYNC_S, fs, phase)
            segs.append(seg)
            seg, phase = _scan(b[row], self._SCAN_B, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_SYNC, self._SYNC_S, fs, phase)
            segs.append(seg)
            seg, phase = _scan(r[row], self._SCAN_R, fs, phase)
            segs.append(seg)

        audio = _concat_segs(segs)
        return np.clip(audio * 32767, -32768, 32767).astype(np.int16)

    def decode(self, freq: np.ndarray, fs: int = FS) -> Optional[np.ndarray]:
        sync_n  = int(round(self._SYNC_S * fs))
        porch_n = int(round(self._PORCH_S * fs))
        g_n     = int(round(self._SCAN_G * fs))
        b_n     = int(round(self._SCAN_B * fs))
        r_n     = int(round(self._SCAN_R * fs))
        line_n  = sync_n + porch_n + g_n + sync_n + b_n + sync_n + r_n

        starts = _find_sync_lines(freq, sync_n, fs, line_n,
                                  offset=_VIS_HEADER_N)
        if not starts:
            return None

        r_arr = np.zeros((self.height, self.width), dtype=np.uint8)
        g_arr = np.zeros((self.height, self.width), dtype=np.uint8)
        b_arr = np.zeros((self.height, self.width), dtype=np.uint8)

        for idx, start in enumerate(starts[:self.height]):
            if start + line_n > len(freq):
                break
            off = start + sync_n + porch_n
            g_arr[idx] = _freqs_to_pixels(freq[off:off + g_n], self.width)
            off += g_n + sync_n
            b_arr[idx] = _freqs_to_pixels(freq[off:off + b_n], self.width)
            off += b_n + sync_n
            r_arr[idx] = _freqs_to_pixels(freq[off:off + r_n], self.width)

        return np.stack([r_arr, g_arr, b_arr], axis=2)


class _MartinM1(_MartinBase):
    vis_code = VIS_MARTIN_M1
    _SYNC_S  = 0.004862
    _SCAN_G  = 0.146432
    _SCAN_B  = 0.146432
    _SCAN_R  = 0.146432


class _MartinM2(_MartinBase):
    vis_code = VIS_MARTIN_M2
    _SYNC_S  = 0.004862
    _SCAN_G  = 0.073216
    _SCAN_B  = 0.073216
    _SCAN_R  = 0.073216


# ---------------------------------------------------------------------------
# Scottie S1 / S2
# ---------------------------------------------------------------------------

class _ScottieBase(_SstvMode):
    """Scottie S1 and S2 — RGB, 320×256.

    Line order: G sync B sync R (leading separator before sync at each line start).
    """
    width    = 320
    height   = 256

    _SYNC_S: float = 0.009
    _SEP_S:  float = 0.001500
    _SCAN_G: float = 0.0
    _SCAN_B: float = 0.0
    _SCAN_R: float = 0.0

    def encode(self, image: np.ndarray, fs: int = FS) -> np.ndarray:
        if image.shape != (self.height, self.width, 3):
            image = _resize_image(image, self.height, self.width)
        r = image[:, :, 0]
        g = image[:, :, 1]
        b = image[:, :, 2]

        segs: list[np.ndarray] = []
        phase = 0.0
        header, phase = _vis_header(self.vis_code, fs)
        segs.append(header)

        # Scottie: first sync before first line only
        seg, phase = _tone(FREQ_SYNC, self._SYNC_S, fs, phase)
        segs.append(seg)

        for row in range(self.height):
            seg, phase = _tone(FREQ_BLACK, self._SEP_S, fs, phase)
            segs.append(seg)
            seg, phase = _scan(g[row], self._SCAN_G, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_BLACK, self._SEP_S, fs, phase)
            segs.append(seg)
            seg, phase = _scan(b[row], self._SCAN_B, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_SYNC, self._SYNC_S, fs, phase)
            segs.append(seg)
            seg, phase = _tone(FREQ_BLACK, self._SEP_S, fs, phase)
            segs.append(seg)
            seg, phase = _scan(r[row], self._SCAN_R, fs, phase)
            segs.append(seg)

        audio = _concat_segs(segs)
        return np.clip(audio * 32767, -32768, 32767).astype(np.int16)

    def decode(self, freq: np.ndarray, fs: int = FS) -> Optional[np.ndarray]:
        sync_n  = int(round(self._SYNC_S * fs))
        sep_n   = int(round(self._SEP_S * fs))
        g_n     = int(round(self._SCAN_G * fs))
        b_n     = int(round(self._SCAN_B * fs))
        r_n     = int(round(self._SCAN_R * fs))
        line_n  = sep_n + g_n + sep_n + b_n + sync_n + sep_n + r_n

        starts = _find_sync_lines(freq, sync_n, fs, line_n,
                                  offset=_VIS_HEADER_N)
        if not starts:
            return None

        r_arr = np.zeros((self.height, self.width), dtype=np.uint8)
        g_arr = np.zeros((self.height, self.width), dtype=np.uint8)
        b_arr = np.zeros((self.height, self.width), dtype=np.uint8)

        for idx, start in enumerate(starts[:self.height]):
            # start points to sync pulse; image data is after it
            off = start + sync_n + sep_n
            if off + g_n + sep_n + b_n + sep_n + r_n > len(freq):
                break
            g_arr[idx] = _freqs_to_pixels(freq[off:off + g_n], self.width)
            off += g_n + sep_n
            b_arr[idx] = _freqs_to_pixels(freq[off:off + b_n], self.width)
            off += b_n + sep_n
            r_arr[idx] = _freqs_to_pixels(freq[off:off + r_n], self.width)

        return np.stack([r_arr, g_arr, b_arr], axis=2)


class _ScottieS1(_ScottieBase):
    vis_code = VIS_SCOTTIE_S1
    _SCAN_G  = 0.138240
    _SCAN_B  = 0.138240
    _SCAN_R  = 0.138240


class _ScottieS2(_ScottieBase):
    vis_code = VIS_SCOTTIE_S2
    _SCAN_G  = 0.088064
    _SCAN_B  = 0.088064
    _SCAN_R  = 0.088064


# ---------------------------------------------------------------------------
# Shared decode helpers
# ---------------------------------------------------------------------------

def _freqs_to_pixels(freq_seg: np.ndarray, width: int) -> np.ndarray:
    """Convert a frequency segment to a pixel row."""
    px = np.clip((freq_seg - FREQ_BLACK) / FREQ_RANGE * 255.0, 0, 255)
    src = np.linspace(0, len(px) - 1, width)
    return np.interp(src, np.arange(len(px)), px).astype(np.uint8)


def _find_sync_lines(freq: np.ndarray, sync_n: int, fs: int,
                     line_n: int, offset: int = 0) -> list[int]:
    """Find scan-line sync pulse positions in a frequency track.

    offset: number of samples at the start to skip (used to bypass the VIS
    header, whose 1200 Hz start/stop bits would otherwise be false positives).
    """
    tol     = 80.0
    n       = len(freq)
    min_run = max(1, int(0.60 * sync_n))
    near    = (np.abs(freq - FREQ_SYNC) < tol).astype(np.uint8)

    starts: list[int] = []
    pos     = offset
    advance = max(1, line_n - sync_n)

    while pos + min_run <= n:
        window = near[pos:pos + min_run]
        if np.sum(window) >= int(0.75 * min_run):
            starts.append(pos)
            pos += advance
        else:
            pos += 1

    return starts


def _resize_image(image: np.ndarray, height: int, width: int) -> np.ndarray:
    """Simple nearest-neighbour resize to (height, width, 3)."""
    h, w = image.shape[:2]
    row_idx = np.round(np.linspace(0, h - 1, height)).astype(int)
    col_idx = np.round(np.linspace(0, w - 1, width)).astype(int)
    return image[np.ix_(row_idx, col_idx)]


# ---------------------------------------------------------------------------
# FM demodulator (shared with fax)
# ---------------------------------------------------------------------------

def _fm_demodulate(audio: np.ndarray, fc: float = 1900.0,
                   fs: int = FS) -> np.ndarray:
    """FM discriminator via analytic-signal complex baseband differentiation.

    Uses an FFT-based Hilbert transform to suppress the real-signal image
    before mixing, giving accurate instantaneous-frequency estimates even
    for closely-spaced tones (e.g. VIS 1200/1300 Hz).
    """
    x = audio.astype(np.float64) / 32768.0
    n = len(x)
    X = np.fft.fft(x)
    H = np.zeros(n)
    H[0] = 1.0
    if n % 2 == 0:
        H[1:n // 2] = 2.0
        H[n // 2]   = 1.0
    else:
        H[1:(n + 1) // 2] = 2.0
    z  = np.fft.ifft(X * H)
    t  = np.arange(n) / fs
    bb = z * np.exp(-2j * np.pi * fc * t)
    diff     = bb[1:] * np.conj(bb[:-1])
    freq_dev = np.angle(diff) * fs / (2.0 * np.pi)
    freq     = fc + freq_dev
    return np.concatenate([[freq[0]], freq])


# ---------------------------------------------------------------------------
# Mode registry
# ---------------------------------------------------------------------------

_MODES: dict[str, _SstvMode] = {
    'robot36':    _Robot36(),
    'martin_m1':  _MartinM1(),
    'martin_m2':  _MartinM2(),
    'scottie_s1': _ScottieS1(),
    'scottie_s2': _ScottieS2(),
}

# VIS header duration in samples (same for every mode)
_VIS_HEADER_S = 0.300 + 0.010 + 0.300 + 10 * 0.030  # leaders + 10 bits
_VIS_HEADER_N = int(round(_VIS_HEADER_S * FS))        # 7280 samples

_VIS_TO_MODE: dict[int, _SstvMode] = {m.vis_code: m for m in _MODES.values()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SstvEncoder:
    """Encode an image to SSTV audio for a given mode.

    Parameters
    ----------
    mode : str
        One of: 'robot36', 'martin_m1', 'martin_m2', 'scottie_s1', 'scottie_s2'.
    fs : int
        Sample rate (default 8000).
    """

    MODES = list(_MODES.keys())

    def __init__(self, mode: str = 'robot36', fs: int = FS) -> None:
        if mode not in _MODES:
            raise ValueError(f"Unknown SSTV mode '{mode}'. Choose from: {self.MODES}")
        self._mode = _MODES[mode]
        self.mode  = mode
        self.fs    = fs

    @property
    def width(self) -> int:
        return self._mode.width

    @property
    def height(self) -> int:
        return self._mode.height

    def encode(self, image: np.ndarray) -> np.ndarray:
        """Encode a H×W×3 uint8 RGB image to int16 audio.

        The image is resized if its dimensions differ from the mode's native size.
        """
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("image must be H×W×3 uint8 RGB")
        return self._mode.encode(image.astype(np.uint8), self.fs)


class SstvDecoder:
    """Decode SSTV audio to an image.

    The VIS code in the header determines the mode automatically.
    """

    def __init__(self, fs: int = FS) -> None:
        self.fs = fs

    def decode(self, audio: np.ndarray) -> Optional[np.ndarray]:
        """Decode int16 SSTV audio → H×W×3 uint8 RGB image, or None.

        The VIS header is located automatically.  Returns None if no
        valid VIS header is found.
        """
        freq = _fm_demodulate(audio, fc=1750.0, fs=self.fs)
        vis  = _decode_vis(freq, self.fs)
        if vis is None or vis not in _VIS_TO_MODE:
            return None
        mode = _VIS_TO_MODE[vis]
        return mode.decode(freq, self.fs)


class Sstv:
    """Combined SSTV encoder / decoder.

    Usage::

        sstv = Sstv('martin_m1')
        audio = sstv.encode(image_array)     # H×W×3 uint8 RGB → int16
        recovered = sstv.decode(audio)        # int16 → H×W×3 uint8 RGB or None
    """

    MODES = SstvEncoder.MODES

    def __init__(self, mode: str = 'robot36', fs: int = FS) -> None:
        self._enc = SstvEncoder(mode, fs)
        self._dec = SstvDecoder(fs)
        self.mode = mode
        self.fs   = fs

    @property
    def width(self) -> int:
        return self._enc.width

    @property
    def height(self) -> int:
        return self._enc.height

    def encode(self, image: np.ndarray) -> np.ndarray:
        return self._enc.encode(image)

    def decode(self, audio: np.ndarray) -> Optional[np.ndarray]:
        return self._dec.decode(audio)
