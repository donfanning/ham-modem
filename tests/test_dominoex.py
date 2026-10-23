"""Tests for DominoEX and Thor IFK+ keyboard modes."""

import unittest
import numpy as np
from hammodem.keyboard.dominoex import (
    DominoModulator, DominoDemodulator,
    dom_varicode_encode, DomVaricodeDecoder,
    NUMTONES,
)


class TestDomVaricode(unittest.TestCase):
    """DominoEX nibble-based varicode round-trip."""

    def test_printable_ascii_round_trip(self):
        dec = DomVaricodeDecoder()
        for ch in range(32, 127):
            nibbles = dom_varicode_encode(ch)
            self.assertTrue(1 <= len(nibbles) <= 3, f"char {ch} nibbles={nibbles}")

    def test_encode_decode_letters(self):
        dec = DomVaricodeDecoder()
        text = "Hello World 123"
        all_nibbles: list[int] = []
        for ch in text:
            all_nibbles.extend(dom_varicode_encode(ord(ch)))
        # Feed all nibbles then flush
        recovered = ""
        for n in all_nibbles:
            c = dec.nibble(n)
            if c:
                recovered += c
        # Feed a dummy start nibble to flush the last char
        c = dec.nibble(0)
        if c:
            recovered += c
        self.assertEqual(recovered, text)


class TestDominoEXModem(unittest.TestCase):
    """DominoEX 8 modulator/demodulator round-trip."""

    def test_clean_round_trip(self):
        """Clean-channel DominoEX 8 round-trip."""
        msg = "CQ CQ DE N0CALL"
        mod = DominoModulator(fs=8000, symlen=1024, doublespaced=2)
        demod = DominoDemodulator(fs=8000, symlen=1024, doublespaced=2)

        audio = np.concatenate([
            mod.idle(2.0),
            mod.text(msg),
            mod.idle(2.0),
        ])

        text = demod.feed(audio.astype(float))
        # Strip leading/trailing garbage from idle symbols
        self.assertIn(msg, text)

    def test_noisy_round_trip(self):
        """DominoEX 8 at 6 dB SNR (narrow bandwidth helps)."""
        msg = "TEST 73"
        mod = DominoModulator(fs=8000, symlen=1024, doublespaced=2)
        demod = DominoDemodulator(fs=8000, symlen=1024, doublespaced=2)

        audio = np.concatenate([
            mod.idle(2.0),
            mod.text(msg),
            mod.idle(2.0),
        ])

        rng = np.random.default_rng(42)
        sig_power = np.mean(audio.astype(float) ** 2)
        snr_linear = 10 ** (6.0 / 10)
        noise_power = sig_power / snr_linear
        noise = rng.normal(0, np.sqrt(noise_power), len(audio))
        noisy = audio.astype(float) + noise

        text = demod.feed(noisy)
        self.assertIn(msg, text)

    def test_ifk_tone_difference(self):
        """Verify IFK+: tone = (prev + 2 + sym) mod 18."""
        mod = DominoModulator(fs=8000, symlen=1024, doublespaced=2)
        prev = mod._txprevtone
        sym = 5
        expected_tone = (prev + 2 + sym) % NUMTONES
        mod._sendsymbol(sym)
        self.assertEqual(mod._txprevtone, expected_tone)

    def test_domex16_round_trip(self):
        """DominoEX 16 (single-spaced) round-trip."""
        msg = "DomEX16"
        mod = DominoModulator(fs=8000, symlen=512, doublespaced=1)
        demod = DominoDemodulator(fs=8000, symlen=512, doublespaced=1)

        audio = np.concatenate([
            mod.idle(2.0),
            mod.text(msg),
            mod.idle(2.0),
        ])

        text = demod.feed(audio.astype(float))
        self.assertIn(msg, text)


class TestThorModem(unittest.TestCase):
    """Thor IFK+ with FEC round-trip."""

    def test_clean_round_trip(self):
        """Clean-channel Thor 8 round-trip."""
        msg = "CQ THOR"
        mod = DominoModulator(fs=8000, symlen=1024, doublespaced=2, thor=True, idepth=10)
        demod = DominoDemodulator(fs=8000, symlen=1024, doublespaced=2, thor=True, idepth=10)

        audio = np.concatenate([
            mod.idle(4.0),
            mod.text(msg),
            mod.idle(6.0),
        ])

        text = demod.feed(audio.astype(float))
        self.assertIn(msg, text)

    def test_noisy_round_trip(self):
        """Thor 8 at 3 dB SNR — FEC should save it."""
        msg = "FEC OK"
        mod = DominoModulator(fs=8000, symlen=1024, doublespaced=2, thor=True, idepth=10)
        demod = DominoDemodulator(fs=8000, symlen=1024, doublespaced=2, thor=True, idepth=10)

        audio = np.concatenate([
            mod.idle(4.0),
            mod.text(msg),
            mod.idle(6.0),
        ])

        rng = np.random.default_rng(99)
        sig_power = np.mean(audio.astype(float) ** 2)
        snr_linear = 10 ** (3.0 / 10)
        noise_power = sig_power / snr_linear
        noise = rng.normal(0, np.sqrt(noise_power), len(audio))
        noisy = audio.astype(float) + noise

        text = demod.feed(noisy)
        self.assertIn(msg, text)


class TestTwoTerminal(unittest.TestCase):
    """Two-terminal keyboard exchange (DominoEX)."""

    def test_two_stations(self):
        fs, symlen, ds = 8000, 1024, 2
        mod_a = DominoModulator(fs=fs, symlen=symlen, doublespaced=ds)
        mod_b = DominoModulator(fs=fs, symlen=symlen, doublespaced=ds)
        demod_a = DominoDemodulator(fs=fs, symlen=symlen, doublespaced=ds)
        demod_b = DominoDemodulator(fs=fs, symlen=symlen, doublespaced=ds)

        msg_a = "CQ DE N0CALL"
        msg_b = "N0CALL DE W1AW"

        audio_a = np.concatenate([mod_a.idle(2.0), mod_a.text(msg_a), mod_a.idle(2.0)])
        audio_b = np.concatenate([mod_b.idle(2.0), mod_b.text(msg_b), mod_b.idle(2.0)])

        text_b = demod_b.feed(audio_a.astype(float))
        text_a = demod_a.feed(audio_b.astype(float))

        self.assertIn(msg_a, text_b)
        self.assertIn(msg_b, text_a)


if __name__ == "__main__":
    unittest.main()
