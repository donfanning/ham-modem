import unittest

import numpy as np

from hammodem.ax25 import Frame
from hammodem.modems import make
from tests import impair


def bursts(name, frames, fs=8000, gap=2000):
    mod, _ = make(name, fs)
    return np.concatenate([np.concatenate((mod.modulate([f]), np.zeros(gap, np.int16))) for f in frames]).astype(float)


def receive(name, audio, fs=8000, block=160):
    _, dem = make(name, fs)
    pcm = impair.to_pcm(audio)
    got = []
    for i in range(0, len(pcm), block):
        got += dem.feed(pcm[i:i + block])
    return got


def frames(n=8):
    return [Frame.ui("N0CALL-1", "APRS", f">psk {i} " + "x" * 60, "WIDE1-1").encode() for i in range(n)]


class PskTest(unittest.TestCase):
    def check(self, name, fn, minimum, fs=8000, gap=2000):
        fs_ = frames()
        got = receive(name, fn(bursts(name, fs_, fs, gap), np.random.default_rng(3)), fs)
        self.assertEqual(len(got), sum(g in fs_ for g in got))
        self.assertGreaterEqual(sum(g in fs_ for g in got), minimum)

    def test_qpsk_clean_noise_drift_gain(self):
        self.check("psk2400", lambda a, r: a, 8)
        self.check("psk2400", lambda a, r: impair.noise(a, 2000, r), 8)       # 3000 rms is the 8 kHz edge
        self.check("psk2400", lambda a, r: impair.drift_bandlimited(a, 300), 8)
        self.check("psk2400", lambda a, r: a * 0.3, 8)

    def test_8psk_clean_and_noise(self):
        self.check("psk4800", lambda a, r: a, 8)
        self.check("psk4800", lambda a, r: impair.noise(a, 1000, r), 7)

    def test_short_gaps_between_packets(self):
        # Direwolf leaves ~10 ms between packets: re-acquisition must not wait for a whole quiet block.
        self.check("psk4800", lambda a, r: a, 8, fs=48000, gap=480)

    def test_noise_filled_gaps(self):
        # With noise in the gaps the onset is relative to the noise floor (and quality-triggered).
        self.check("psk4800", lambda a, r: impair.noise(a, 800, r), 7, gap=2000)

    def test_48k(self):
        self.check("psk2400", lambda a, r: a, 8, fs=48000)


class NinoTncPskTest(unittest.TestCase):
    """Round-trip tests for NinoTNC's BPSK/QPSK modes (IL2Pc framing)."""

    def _roundtrip(self, name, fs=8000, n=4, txdelay=0.5):
        fs_ = [Frame.ui("N0CALL", "APRS", f">nino {i} " + "y" * 30).encode() for i in range(n)]
        mod, dem = make(name, fs)
        audio = np.concatenate([
            np.concatenate((mod.modulate([f], txdelay=txdelay), np.zeros(int(fs * 0.1), np.int16)))
            for f in fs_
        ])
        got = []
        for i in range(0, len(audio), 160):
            got += dem.feed(audio[i:i + 160])
        return fs_, got

    def test_ninobpsk300_roundtrip(self):
        fs_, got = self._roundtrip("ninobpsk300", txdelay=0.5)
        self.assertEqual(got, fs_)

    def test_ninobpsk1200_roundtrip(self):
        fs_, got = self._roundtrip("ninobpsk1200")
        self.assertEqual(got, fs_)

    def test_ninopsk600_roundtrip(self):
        fs_, got = self._roundtrip("ninopsk600", txdelay=0.5)
        self.assertEqual(got, fs_)

    def test_ninopsk3600_roundtrip(self):
        fs_, got = self._roundtrip("ninopsk3600")
        self.assertEqual(got, fs_)

    def test_ninotnc_crc_mode_rejects_plain_il2p(self):
        """A NinoTNC demodulator (crc_mode=True) drops frames encoded without CRC."""
        f = Frame.ui("N0CALL", "APRS", ">no crc").encode()
        # Build plain IL2P audio on 1500 Hz carrier BPSK 300
        from hammodem.modems.psk import PskModulator, NINO_BPSK300
        mod = PskModulator(NINO_BPSK300, 8000)
        mod.il2p = True            # plain IL2P, no CRC
        mod.il2p_crc = False
        audio = mod.modulate([f], txdelay=0.5)
        from hammodem.modems.psk import PskDemodulator
        dem = PskDemodulator(NINO_BPSK300, 8000, il2p_crc=True)
        got = []
        for i in range(0, len(audio), 160):
            got += dem.feed(audio[i:i + 160])
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
