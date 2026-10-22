import unittest

import numpy as np

from hammodem.audio import g711

try:
    from hammodem.freedata.codec2 import MODES, Datac
    Datac("datac0").close()
    HAVE = True
except OSError:
    HAVE = False


def ready(mode):
    try:
        Datac(mode).close()
        return True
    except OSError:
        return False


@unittest.skipUnless(HAVE, "libcodec2 not installed")
class DatacTest(unittest.TestCase):
    def roundtrip(self, mode, n_frames, through_g711=False, noise=0.0, pad=1234):
        tx = Datac(mode, n_frames)
        payloads = [bytes((i * 31 + k) % 256 for k in range(tx.payload_bytes)) for i in range(n_frames)]
        x = tx.modulate(payloads).astype(float)
        x = np.concatenate((np.zeros(pad), x, np.zeros(16000)))
        if through_g711:
            x = g711.decode(g711.encode(x.astype(np.int16), "pcmu"), "pcmu").astype(float)
        if noise:
            x = x + np.random.default_rng(3).normal(0, noise, len(x))
        got = Datac(mode, n_frames).feed(np.round(x).astype(np.int16))
        return payloads, got

    def test_frame_sizes(self):
        for mode, size in (("datac0", 14), ("datac1", 510), ("datac3", 126), ("datac4", 54), ("datac13", 14)):
            d = Datac(mode)
            self.assertEqual(d.payload_bytes, size, mode)

    def test_every_mode_through_ulaw(self):
        for mode in MODES:
            if not ready(mode):
                continue
            with self.subTest(mode=mode):
                payloads, got = self.roundtrip(mode, 2, through_g711=True)
                self.assertEqual([g.payload for g in got if g.crc_ok], payloads)

    def test_survives_moderate_noise(self):
        payloads, got = self.roundtrip("datac3", 2, noise=1500)         # signal peaks near 16000
        self.assertEqual([g.payload for g in got if g.crc_ok], payloads)

    def test_oversized_payload_is_refused(self):
        with self.assertRaises(ValueError):
            Datac("datac0").modulate([bytes(15)])


if __name__ == "__main__":
    unittest.main()
