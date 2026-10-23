"""Tests for JS8 keyboard mode."""

import unittest
import numpy as np
from hammodem.keyboard.js8 import (
    Js8Modulator, Js8Demodulator,
    _pack_message, _unpack_message, _bytes_to_bits, _bits_to_bytes,
    ldpc_encode, encode_frame, _H, _Nm, _Mn_full,
    COSTAS_ORIGINAL, COSTAS_MODIFIED,
)


class TestCrc12(unittest.TestCase):
    def test_pack_unpack(self):
        text = "CQ-JS8-N0CAL"
        buf = _pack_message(text)
        text2, ft, ok = _unpack_message(buf)
        self.assertTrue(ok)
        self.assertEqual(ft, 0)
        self.assertEqual(text2[:12], text[:12])

    def test_crc_detects_corruption(self):
        buf = bytearray(_pack_message("HELLO-WORLD0"))
        buf[3] ^= 0x10
        _, _, ok = _unpack_message(bytes(buf))
        self.assertFalse(ok)

    def test_frame_type(self):
        for ft in range(8):
            buf = _pack_message("TEST00000000", ft)
            _, ft2, ok = _unpack_message(buf)
            self.assertTrue(ok)
            self.assertEqual(ft2, ft)


class TestLdpc(unittest.TestCase):
    def test_encode_syndrome(self):
        """LDPC codeword passes syndrome check."""
        buf = _pack_message("ABCDEFGHIJKL")
        info_bits = _bytes_to_bits(buf, 87)
        cw = ldpc_encode(info_bits)
        self.assertEqual(len(cw), 174)
        H_full = np.zeros((87, 174), dtype=np.uint8)
        H_full[:, :87] = np.eye(87, dtype=np.uint8)
        H_full[:, 87:] = _H
        syndrome = H_full @ np.array(cw, dtype=np.uint8) % 2
        self.assertTrue(np.all(syndrome == 0))

    def test_encode_decode_clean(self):
        """BP decoder recovers from clean LLRs."""
        from hammodem.keyboard.js8 import _bp_decode
        buf = _pack_message("CQ-JS8-N0CAL")
        info_bits = _bytes_to_bits(buf, 87)
        cw = ldpc_encode(info_bits)
        llr = np.array([10.0 if b == 0 else -10.0 for b in cw])
        decoded, iters = _bp_decode(llr, max_iter=10)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded, info_bits)
        self.assertLessEqual(iters, 2)


class TestToneEncoding(unittest.TestCase):
    def test_frame_length(self):
        tones = encode_frame("TEST00000000")
        self.assertEqual(len(tones), 79)

    def test_costas_positions(self):
        tones = encode_frame("TEST00000000")
        # Costas at positions 0-6, 36-42, 72-78
        for i in range(7):
            self.assertEqual(tones[i], COSTAS_ORIGINAL[0][i])
            self.assertEqual(tones[36 + i], COSTAS_ORIGINAL[1][i])
            self.assertEqual(tones[72 + i], COSTAS_ORIGINAL[2][i])

    def test_tone_range(self):
        tones = encode_frame("Hello-World0")
        for t in tones:
            self.assertGreaterEqual(int(t), 0)
            self.assertLessEqual(int(t), 7)


class TestJs8Modem(unittest.TestCase):
    def test_clean_round_trip(self):
        """Clean-channel JS8 Normal round-trip."""
        msg = "CQ-JS8-N0CAL"
        mod = Js8Modulator(fs=8000, symlen=1280)
        demod = Js8Demodulator(fs=8000, symlen=1280)

        audio = np.concatenate([
            mod.idle(2.0),
            mod.frame(msg),
            mod.idle(2.0),
        ])
        text = demod.feed(audio.astype(float))
        self.assertIn(msg, text)

    def test_noisy_round_trip(self):
        """JS8 Normal at -5 dB SNR — LDPC should save it."""
        msg = "FEC-WORKS000"
        mod = Js8Modulator(fs=8000, symlen=1280)
        demod = Js8Demodulator(fs=8000, symlen=1280)

        audio = np.concatenate([
            mod.idle(2.0),
            mod.frame(msg),
            mod.idle(2.0),
        ])
        rng = np.random.default_rng(42)
        sig_power = np.mean(audio[audio != 0].astype(float) ** 2)
        snr_linear = 10 ** (-5.0 / 10)
        noise_power = sig_power / snr_linear
        noise = rng.normal(0, np.sqrt(noise_power), len(audio))
        noisy = audio.astype(float) + noise

        text = demod.feed(noisy)
        self.assertIn("FEC-WORKS", text)

    def test_multi_frame(self):
        """Two consecutive 12-char frames."""
        mod = Js8Modulator(fs=8000, symlen=1280)
        demod = Js8Demodulator(fs=8000, symlen=1280)

        msg1 = "FRAME1-ABCDE"
        msg2 = "FRAME2-FGHIJ"
        audio = np.concatenate([
            mod.idle(2.0),
            mod.frame(msg1),
            mod.frame(msg2),
            mod.idle(2.0),
        ])
        text = demod.feed(audio.astype(float))
        self.assertIn("FRAME1-ABCDE", text)
        self.assertIn("FRAME2-FGHIJ", text)

    def test_alphabet(self):
        """All 64 alphabet characters encode/decode."""
        from hammodem.keyboard.js8 import ALPHABET
        for i in range(0, 64, 12):
            chunk = ALPHABET[i:i+12].ljust(12, '0')[:12]
            buf = _pack_message(chunk)
            text, _, ok = _unpack_message(buf)
            self.assertTrue(ok)
            self.assertEqual(text, chunk)


class TestTwoTerminal(unittest.TestCase):
    def test_two_stations(self):
        fs, sl = 8000, 1280
        mod_a = Js8Modulator(fs=fs, symlen=sl)
        mod_b = Js8Modulator(fs=fs, symlen=sl)
        demod_a = Js8Demodulator(fs=fs, symlen=sl)
        demod_b = Js8Demodulator(fs=fs, symlen=sl)

        msg_a = "CQ-DE-N0CALL"
        msg_b = "N0CALL-W1AW0"

        audio_a = np.concatenate([mod_a.idle(2.0), mod_a.frame(msg_a), mod_a.idle(2.0)])
        audio_b = np.concatenate([mod_b.idle(2.0), mod_b.frame(msg_b), mod_b.idle(2.0)])

        text_b = demod_b.feed(audio_a.astype(float))
        text_a = demod_a.feed(audio_b.astype(float))

        self.assertIn(msg_a, text_b)
        self.assertIn("N0CALL-W1AW", text_a)


if __name__ == "__main__":
    unittest.main()
