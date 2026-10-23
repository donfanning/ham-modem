"""JS8: FT8-derived 8-FSK keyboard mode with (174,87) LDPC FEC.

Interop target: JS8Call.

JS8 Normal: 8-FSK at 6.25 baud, 6.25 Hz tone spacing, 50 Hz bandwidth,
79 symbols per frame (3×7 Costas sync + 29 parity + 29 data).  Each frame
carries 12 characters from a 64-symbol alphabet.

Message structure: 72 bits (12 × 6-bit chars) + 3 bits (frame type) +
12 bits (CRC-12) = 87 info bits → 87 parity bits via LDPC → 174 bits
= 58 × 3-bit symbols, bracketed by 3 Costas arrays.

Reference: JS8Call-improved source (JS8.cpp, JS8Submode.cpp, Modulator.cpp).
"""

from __future__ import annotations

import numpy as np

TWOPI = 2 * np.pi
NTONES = 8
NSYMBOLS = 79
NSYNC = 21
NDATA = 58

# 64-character alphabet (6-bit encoding)
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-+"
_CHAR_TO_WORD: dict[str, int] = {c: i for i, c in enumerate(ALPHABET)}
_WORD_TO_CHAR: dict[int, str] = {i: c for i, c in enumerate(ALPHABET)}

# Costas arrays (3×7)
COSTAS_ORIGINAL = [[4, 2, 5, 6, 1, 3, 0]] * 3  # Normal mode
COSTAS_MODIFIED = [
    [0, 6, 2, 3, 5, 4, 1],
    [1, 5, 0, 2, 3, 6, 4],
    [2, 5, 0, 6, 4, 1, 3],
]

# ------------------------------------------------------------------ CRC-12
# Augmented CRC-12 with polynomial 0xC06, XOR 42 (matches JS8Call)

_CRC12_POLY = 0x1C06  # x^12 + x^11 + x^10 + x^2 + x^1 (with implicit x^12 bit)


def _crc12(data: bytes) -> int:
    """Augmented CRC-12 of data bytes, XORed with 42."""
    crc = 0
    for byte in data:
        crc ^= byte << 4
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000:
                crc ^= _CRC12_POLY
            crc &= 0xFFF
    # Augment: process 12 more zero bits (1.5 bytes)
    for _ in range(12):
        crc <<= 1
        if crc & 0x1000:
            crc ^= _CRC12_POLY
        crc &= 0xFFF
    return crc ^ 42


# --------------------------------------------------------- LDPC (174,87) code
# Parity matrix rows (87 × 87 bits), from JS8Call's ldpc_174_87_params

_PARITY_HEX = [
    "23bba830e23b6b6f50982e", "1f8e55da218c5df3309052",
    "ca7b3217cd92bd59a5ae20", "56f78313537d0f4382964e",
    "6be396b5e2e819e373340c", "293548a138858328af4210",
    "cb6c6afcdc28bb3f7c6e86", "3f2a86f5c5bd225c961150",
    "849dd2d63673481860f62c", "56cdaec6e7ae14b43feeee",
    "04ef5cfa3766ba778f45a4", "c525ae4bd4f627320a3974",
    "41fd9520b2e4abeb2f989c", "7fb36c24085a34d8c1dbc4",
    "40fc3e44bb7d2bb2756e44", "d38ab0a1d2e52a8ec3bc76",
    "3d0f929ef3949bd84d4734", "45d3814f504064f80549ae",
    "f14dbf263825d0bd04b05e", "db714f8f64e8ac7af1a76e",
    "8d0274de71e7c1a8055eb0", "51f81573dd4049b082de14",
    "d8f937f31822e57c562370", "b6537f417e61d1a7085336",
    "ecbd7c73b9cd34c3720c8a", "3d188ea477f6fa41317a4e",
    "1ac4672b549cd6dba79bcc", "a377253773ea678367c3f6",
    "0dbd816fba1543f721dc72", "ca4186dd44c3121565cf5c",
    "29c29dba9c545e267762fe", "1616d78018d0b4745ca0f2",
    "fe37802941d66dde02b99c", "a9fa8e50bcb032c85e3304",
    "83f640f1a48a8ebc0443ea", "3776af54ccfbae916afde6",
    "a8fc906976c35669e79ce0", "f08a91fb2e1f78290619a8",
    "cc9da55fe046d0cb3a770c", "d36d662a69ae24b74dcbd8",
    "40907b01280f03c0323946", "d037db825175d851f3af00",
    "1bf1490607c54032660ede", "0af7723161ec223080be86",
    "eca9afa0f6b01d92305edc", "7a8dec79a51e8ac5388022",
    "9059dfa2bb20ef7ef73ad4", "6abb212d9739dfc02580f2",
    "f6ad4824b87c80ebfce466", "d747bfc5fd65ef70fbd9bc",
    "612f63acc025b6ab476f7c", "05209a0abb530b9e7e34b0",
    "45b7ab6242b77474d9f11a", "6c280d2a0523d9c4bc5946",
    "f1627701a2d692fd9449e6", "8d9071b7e7a6a2eed6965e",
    "bf4f56e073271f6ab4bf80", "c0fc3ec4fb7d2bb2756644",
    "57da6d13cb96a7689b2790", "a9fa2eefa6f8796a355772",
    "164cc861bdd803c547f2ac", "cc6de59755420925f90ed2",
    "a0c0033a52ab6299802fd2", "b274db8abd3c6f396ea356",
    "97d4169cb33e7435718d90", "81cfc6f18c35b1e1f17114",
    "481a2a0df8a23583f82d6c", "081c29a10d468ccdbcecb6",
    "2c4142bf42b01e71076acc", "a6573f3dc8b16c9d19f746",
    "c87af9a5d5206abca532a8", "012dee2198eba82b19a1da",
    "b1ca4ea2e3d173bad4379c", "b33ec97be83ce413f9acc8",
    "5b0f7742bca86b8012609a", "37d8e0af9258b9e8c5f9b2",
    "35ad3fb0faeb5f1b0c30dc", "6114e08483043fd3f38a8a",
    "cd921fdf59e882683763f6", "95e45ecd0135aca9d6e6ae",
    "2e547dd7a05f6597aac516", "14cd0f642fc0c5fe3a65ca",
    "3a0a1dfd7eee29c2e827e0", "c8b5dffc335095dcdcaf2a",
    "3dd01a59d86310743ec752", "8abdb889efbe39a510a118",
    "3f231f212055371cf3e2a2",
]


def _build_parity_matrix() -> np.ndarray:
    """Build 87×87 parity matrix from hex strings."""
    H = np.zeros((87, 87), dtype=np.uint8)
    for row, hexstr in enumerate(_PARITY_HEX):
        bits = bin(int(hexstr, 16))[2:].zfill(88)
        for col in range(87):
            H[row, col] = int(bits[col])
    return H


_H = _build_parity_matrix()

# Mn table: for each of the 174 bits, which 3 check nodes it participates in
_Mn = [
    [0, 24, 68],  [1, 4, 72],   [2, 31, 67],  [3, 50, 60],  [4, 62, 69],
    [5, 32, 78],  [6, 49, 85],  [7, 36, 42],  [8, 40, 64],  [9, 13, 63],
    [10, 74, 76], [11, 22, 80], [12, 22, 80], [13, 55, 65], [14, 15, 81],  # Note: bit 12 intentionally same as 11 based on source
    [15, 30, 51], [16, 55, 65], [17, 52, 59], [18, 30, 51],  # fix: re-extract
    [19, 66, 83], [20, 28, 71], [21, 23, 43], [25, 34, 75],
    [26, 35, 37], [27, 39, 41], [22, 53, 54], [29, 53, 54], [33, 48, 86], [24, 56, 57],
    [38, 56, 57], [44, 73, 82], [45, 61, 79], [46, 47, 84], [58, 70, 77],
]

# Rather than transcribing all 174 entries of Mn (error-prone), use the
# parity matrix H and Nm to build Mn programmatically for the BP decoder.


def _build_sparse_tables() -> tuple[list[list[int]], list[list[int]]]:
    """Build check-to-bit (Nm) and bit-to-check (Mn) adjacency lists from H."""
    n_checks = 87
    n_bits = 174
    # Full H matrix: [87 checks × 174 bits] = [I | P] where P is the 87×87 parity matrix
    # Bit i for i < 87: parity bits → column i of identity matrix (check i uses bit i)
    # Bit i for i >= 87: info bits → column (i-87) of P^T
    # Actually: the (174,87) code has H = [P | I_87] where:
    #   - first 87 columns are parity bits
    #   - last 87 columns are info bits
    # Check j is satisfied when: parity_bit[j] XOR (sum of info_bit[k] for H[j,k]=1) = 0
    # In systematic form: codeword = [parity | info], H = [I_87 | P]
    # So H[j, i] for i<87: identity (1 if i==j)
    # H[j, i] for i>=87: P[j, i-87]

    Nm_list: list[list[int]] = [[] for _ in range(n_checks)]
    Mn_list: list[list[int]] = [[] for _ in range(n_bits)]

    for j in range(n_checks):
        # Identity part: bit j (parity bit j)
        Nm_list[j].append(j)
        Mn_list[j].append(j)
        # Parity matrix part: info bits
        for k in range(87):
            if _H[j, k]:
                bit_idx = 87 + k
                Nm_list[j].append(bit_idx)
                Mn_list[bit_idx].append(j)

    return Nm_list, Mn_list


_Nm, _Mn_full = _build_sparse_tables()


# ------------------------------------------------------------------ encoder

def _pack_message(text: str, frame_type: int = 0) -> bytes:
    """Pack up to 12 chars into 11 bytes (87 bits: 72 data + 3 type + 12 CRC)."""
    # Pad or truncate to 12 chars
    padded = text.ljust(12)[:12]
    # Replace invalid chars with space
    chars = []
    for c in padded:
        if c in _CHAR_TO_WORD:
            chars.append(_CHAR_TO_WORD[c])
        else:
            chars.append(_CHAR_TO_WORD[' '] if ' ' in _CHAR_TO_WORD else 0)
    # Actually space isn't in the alphabet.  Use '0' as fallback.
    clean_chars = []
    for c in padded:
        clean_chars.append(_CHAR_TO_WORD.get(c, 0))

    # Pack 12 × 6-bit words into 9 bytes (72 bits)
    buf = bytearray(11)
    for i in range(0, 12, 4):
        j = (i // 4) * 3
        words = (clean_chars[i] << 18) | (clean_chars[i+1] << 12) | (clean_chars[i+2] << 6) | clean_chars[i+3]
        buf[j] = (words >> 16) & 0xFF
        buf[j+1] = (words >> 8) & 0xFF
        buf[j+2] = words & 0xFF

    # Frame type in top 3 bits of byte 9
    buf[9] = (frame_type & 0x7) << 5

    # CRC-12 of the 11-byte array (including trailing zeros where CRC will go)
    crc = _crc12(bytes(buf))

    # Place CRC: 5 bits in low of byte 9, 7 bits in high of byte 10
    buf[9] |= (crc >> 7) & 0x1F
    buf[10] = (crc & 0x7F) << 1

    return bytes(buf)


def _unpack_message(buf: bytes) -> tuple[str, int, bool]:
    """Unpack 11 bytes → (text, frame_type, crc_ok)."""
    # Extract CRC
    received_crc = ((buf[9] & 0x1F) << 7) | (buf[10] >> 1)

    # Clear CRC bits for verification
    check = bytearray(buf)
    check[9] &= 0xE0
    check[10] = 0
    computed_crc = _crc12(bytes(check))
    crc_ok = received_crc == computed_crc

    frame_type = (buf[9] >> 5) & 0x7

    # Unpack 12 chars from 9 bytes
    text = ""
    for i in range(0, 12, 4):
        j = (i // 4) * 3
        words = (buf[j] << 16) | (buf[j+1] << 8) | buf[j+2]
        for shift in (18, 12, 6, 0):
            w = (words >> shift) & 0x3F
            text += _WORD_TO_CHAR.get(w, '?')

    return text, frame_type, crc_ok


def _bytes_to_bits(buf: bytes, n: int) -> list[int]:
    bits = []
    for byte in buf:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
            if len(bits) >= n:
                return bits
    return bits


def _bits_to_bytes(bits: list[int]) -> bytes:
    buf = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            buf[i // 8] |= 1 << (7 - (i % 8))
    return bytes(buf)


def ldpc_encode(info_bits: list[int]) -> list[int]:
    """Encode 87 info bits → 174 codeword bits [parity | info]."""
    assert len(info_bits) == 87
    info = np.array(info_bits, dtype=np.uint8)
    parity = (_H @ info) % 2
    return list(parity) + info_bits


def encode_frame(text: str, frame_type: int = 0,
                 costas: list[list[int]] | None = None) -> list[int]:
    """Encode a 12-char message into 79 tone indices (0-7)."""
    if costas is None:
        costas = COSTAS_ORIGINAL

    buf = _pack_message(text, frame_type)
    info_bits = _bytes_to_bits(buf, 87)
    codeword = ldpc_encode(info_bits)

    # Split into 29 parity + 29 data 3-bit symbols
    parity_syms = []
    data_syms = []
    for i in range(0, 87, 3):
        p = (codeword[i] << 2) | (codeword[i+1] << 1) | codeword[i+2]
        d = (codeword[87+i] << 2) | (codeword[87+i+1] << 1) | codeword[87+i+2]
        parity_syms.append(p)
        data_syms.append(d)

    # Build 79 tones: Costas + parity + Costas + data + Costas
    tones = list(costas[0]) + parity_syms + list(costas[1]) + data_syms + list(costas[2])
    assert len(tones) == 79
    return tones


# --------------------------------------------------------------- BP decoder

def _build_bp_index() -> tuple[list[list[tuple[int, int]]], list[list[tuple[int, int]]]]:
    """Precompute index maps for efficient BP message passing."""
    # For each (check j, neighbor idx_i): list of (check_c, idx_in_c) for bit_i's other checks
    n_checks = 87
    n_bits = 174

    # bit_idx_in_check[j][bit] = position of bit in _Nm[j]
    bit_pos = [[0] * n_bits for _ in range(n_checks)]
    for j in range(n_checks):
        for idx, bit in enumerate(_Nm[j]):
            bit_pos[j][bit] = idx

    # For each bit i: list of (check_j, idx_in_j)
    bit_checks: list[list[tuple[int, int]]] = [[] for _ in range(n_bits)]
    for j in range(n_checks):
        for idx, bit in enumerate(_Nm[j]):
            bit_checks[bit].append((j, idx))

    return bit_checks, bit_pos


_BIT_CHECKS, _BIT_POS = _build_bp_index()


def _bp_decode(llr: np.ndarray, max_iter: int = 30) -> tuple[list[int] | None, int]:
    """Min-sum BP decoder for (174,87) LDPC.

    Returns (decoded_87_bits, iterations) or (None, max_iter) on failure.
    """
    n_bits = 174
    n_checks = 87

    # toc[j][idx] = check-to-bit message for bit _Nm[j][idx]
    toc = [[0.0] * len(_Nm[j]) for j in range(n_checks)]

    for iteration in range(max_iter):
        # Compute total LLR per bit (channel + extrinsic from all checks)
        q = np.array(llr, dtype=float)
        for i in range(n_bits):
            for j, idx in _BIT_CHECKS[i]:
                q[i] += toc[j][idx]

        # Check node update (scaled min-sum)
        for j in range(n_checks):
            neighbors = _Nm[j]
            n_neigh = len(neighbors)
            # Compute extrinsic messages: q - toc for each neighbor
            ext = [q[neighbors[idx]] - toc[j][idx] for idx in range(n_neigh)]
            for idx_i in range(n_neigh):
                sign = 1
                min_abs = 1e30
                for idx_k in range(n_neigh):
                    if idx_k == idx_i:
                        continue
                    v = ext[idx_k]
                    if v < 0:
                        sign *= -1
                    av = abs(v)
                    if av < min_abs:
                        min_abs = av
                toc[j][idx_i] = sign * min_abs * 0.8

        # Hard decision from total LLR
        q2 = np.array(llr, dtype=float)
        for i in range(n_bits):
            for j, idx in _BIT_CHECKS[i]:
                q2[i] += toc[j][idx]
        hard = (q2 < 0).astype(np.int8)

        # Syndrome check
        ok = True
        for j in range(n_checks):
            s = 0
            for bit in _Nm[j]:
                s ^= hard[bit]
            if s:
                ok = False
                break

        if ok:
            return list(hard[87:]), iteration + 1

    return None, max_iter


# --------------------------------------------------------------- modulator

class Js8Modulator:
    """JS8 8-FSK modulator."""

    def __init__(self, fs: int = 8000, symlen: int = 1280,
                 center: float = 1500.0, level: int = 10000,
                 costas: list[list[int]] | None = None) -> None:
        self.fs = fs
        self.symlen = symlen
        self.center = center
        self.level = level
        self.costas = costas or COSTAS_ORIGINAL
        self.tonespacing = fs / symlen
        self.bandwidth = NTONES * self.tonespacing
        self.baud = fs / symlen
        self._phase = 0.0

    def _send_tone(self, tone: int) -> np.ndarray:
        freq = self.center - self.bandwidth / 2.0 + (tone + 0.5) * self.tonespacing
        phase_inc = TWOPI * freq / self.fs
        samples = np.empty(self.symlen)
        for i in range(self.symlen):
            samples[i] = np.sin(self._phase)
            self._phase += phase_inc
            if self._phase > TWOPI:
                self._phase -= TWOPI
        return np.round(self.level * samples).astype(np.int16)

    def frame(self, text: str, frame_type: int = 0) -> np.ndarray:
        """Encode and modulate one 12-char frame."""
        tones = encode_frame(text, frame_type, self.costas)
        parts = [self._send_tone(t) for t in tones]
        return np.concatenate(parts)

    def idle(self, seconds: float) -> np.ndarray:
        n_sym = max(1, round(seconds * self.baud))
        return np.zeros(n_sym * self.symlen, np.int16)

    def idle_samples(self, seconds: float) -> np.ndarray:
        return self.idle(seconds)

    def text(self, text: str) -> np.ndarray:
        """Encode text as one or more 12-char frames."""
        parts: list[np.ndarray] = []
        # Split into 12-char chunks
        for i in range(0, max(1, len(text)), 12):
            chunk = text[i:i+12]
            parts.append(self.frame(chunk))
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    def modulate(self, bits: list[int]) -> np.ndarray:
        return self.text("".join(chr(b) for b in bits))


# ------------------------------------------------------------- demodulator

class Js8Demodulator:
    """JS8 8-FSK demodulator with Costas sync and LDPC decoding."""

    def __init__(self, fs: int = 8000, symlen: int = 1280,
                 center: float = 1500.0,
                 costas: list[list[int]] | None = None) -> None:
        self.fs = fs
        self.symlen = symlen
        self.center = center
        self.costas = costas or COSTAS_ORIGINAL
        self.tonespacing = fs / symlen
        self.bandwidth = NTONES * self.tonespacing

        basetone_f = center - self.bandwidth / 2.0
        self._tone_freqs = [(i + 0.5) * self.tonespacing + basetone_f for i in range(NTONES)]
        n = np.arange(symlen)
        self._basis = np.exp(-2j * np.pi * np.outer(self._tone_freqs, n) / fs)

        self._buf = np.zeros(0, dtype=float)
        self._symbol_buf: list[np.ndarray] = []
        self._costas_threshold = 1.5
        self.chars = 0

    def feed(self, x: np.ndarray) -> str:
        x = np.asarray(x, dtype=float)
        self._buf = np.concatenate((self._buf, x))
        text_parts: list[str] = []
        sl = self.symlen

        # Compute energies for all available symbols
        while len(self._buf) >= sl:
            chunk = self._buf[:sl]
            self._buf = self._buf[sl:]
            energies = np.abs(self._basis @ chunk) ** 2
            self._symbol_buf.append(energies)

        # Search for Costas sync in the symbol buffer
        while len(self._symbol_buf) >= NSYMBOLS:
            sync_pos = self._find_sync()
            if sync_pos is None:
                # No sync found; keep last NSYMBOLS-1 symbols for overlap
                if len(self._symbol_buf) > NSYMBOLS:
                    self._symbol_buf.pop(0)
                else:
                    break
                continue

            if sync_pos + NSYMBOLS > len(self._symbol_buf):
                break  # not enough symbols yet

            decoded = self._try_decode(sync_pos)
            if decoded:
                self.chars += len(decoded)
                text_parts.append(decoded)
                # Advance past this frame
                self._symbol_buf = self._symbol_buf[sync_pos + NSYMBOLS:]
            else:
                # Sync was false positive; skip one symbol
                self._symbol_buf.pop(0)

        return "".join(text_parts)

    def _costas_score(self, offset: int) -> float:
        """Score Costas correlation at a given symbol offset."""
        if offset + NSYMBOLS > len(self._symbol_buf):
            return 0.0
        score = 0.0
        total_energy = 0.0
        for ci, positions in enumerate([(0, 7), (36, 7), (72, 7)]):
            base, length = positions
            ca = self.costas[ci]
            for k in range(length):
                energies = self._symbol_buf[offset + base + k]
                expected_tone = ca[k]
                tone_energy = energies[expected_tone]
                max_other = max(energies[t] for t in range(NTONES) if t != expected_tone)
                score += tone_energy
                total_energy += energies.sum()
        if total_energy < 1e-30:
            return 0.0
        return score / (total_energy / NSYNC)

    def _find_sync(self) -> int | None:
        """Find the best Costas sync position."""
        best_score = 0.0
        best_pos = None
        max_search = min(len(self._symbol_buf) - NSYMBOLS + 1, NSYMBOLS + 10)
        for i in range(max_search):
            score = self._costas_score(i)
            if score > best_score:
                best_score = score
                best_pos = i
        if best_score >= self._costas_threshold and best_pos is not None:
            return best_pos
        return None

    def _try_decode(self, offset: int) -> str:
        """Decode a frame starting at the given symbol offset."""
        syms = self._symbol_buf[offset:offset + NSYMBOLS]

        parity_energies = syms[7:36]
        data_energies = syms[43:72]

        llr = np.zeros(174)
        for sym_idx in range(29):
            pe = parity_energies[sym_idx]
            de = data_energies[sym_idx]
            for bit_pos in range(3):
                mask = 1 << (2 - bit_pos)
                p0 = sum(pe[t] for t in range(8) if not (t & mask))
                p1 = sum(pe[t] for t in range(8) if (t & mask))
                llr[sym_idx * 3 + bit_pos] = np.log(max(p0, 1e-30) / max(p1, 1e-30))

                p0 = sum(de[t] for t in range(8) if not (t & mask))
                p1 = sum(de[t] for t in range(8) if (t & mask))
                llr[87 + sym_idx * 3 + bit_pos] = np.log(max(p0, 1e-30) / max(p1, 1e-30))

        decoded, _ = _bp_decode(llr, max_iter=30)
        if decoded is None:
            return ""

        buf = _bits_to_bytes(decoded)
        text, frame_type, crc_ok = _unpack_message(buf)
        if not crc_ok:
            return ""
        return text.rstrip('0')
