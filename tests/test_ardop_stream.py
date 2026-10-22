import unittest

import numpy as np

from hammodem.ardop import codec, modulate as m
from hammodem.ardop.stream import Phy, Resampler
from hammodem.audio import g711


class ResamplerTest(unittest.TestCase):
    def test_chunked_equals_one_shot_and_is_accurate(self):
        x = 10000 * np.sin(2 * np.pi * 1000 * np.arange(8000) / 8000)
        a = Resampler(12000, 8000).process(x)
        r = Resampler(12000, 8000)
        b = np.concatenate([r.process(x[i:i + 157]) for i in range(0, len(x), 157)])
        np.testing.assert_array_equal(a, b)
        ref = 10000 * np.sin(2 * np.pi * 1000 * (np.arange(len(a)) - r.delay) / 12000)
        self.assertLess(np.max(np.abs(a[200:-200] - ref[200:-200])), 5)


class PhyStreamTest(unittest.TestCase):
    MODES = ("4FSK.200.50S.E", "4PSK.500.100.O", "8PSK.1000.100.E", "16QAM.2000.100.E", "4FSK.2000.600S.O")

    def stream(self, fs, law=None, block=None):
        rng = np.random.default_rng(4)
        tx, sent, parts = Phy(fs), [], [np.zeros(fs // 3, np.int16)]
        for name in self.MODES:
            info = codec.BY_NAME[name]
            data = rng.integers(0, 256, info.data_len * info.carriers, dtype=np.uint8).tobytes()
            sent.append((name, data))
            parts += [tx.transmit(m.encode_fsk_data(info.code, data, 0x5A)), np.zeros(fs // 4, np.int16)]
        for name, enc in (("ConReq500M", m.encode_conreq("N0CALL", "K1ABC-1")),
                          ("DataACK q26", m.encode_dataack(90, 0x5A))):
            sent.append((name, None))
            parts += [tx.transmit(enc), np.zeros(fs // 4, np.int16)]
        s = np.concatenate(parts)
        if law:
            s = g711.decode(g711.encode(s, law), law)
        rx, got = Phy(fs, sessions=(0x5A,)), []
        block = block or fs // 50
        for i in range(0, len(s), block):
            got += rx.feed(s[i:i + block])
        self.assertEqual([d.name for d in got], [n for n, _ in sent])
        for (name, data), d in zip(sent, got):
            self.assertTrue(d.ok, name)
            if data is not None:
                self.assertEqual(d.data, data, name)
        return got

    def test_pcmu_8k_in_rtp_sized_blocks(self):
        got = self.stream(8000, "pcmu")
        self.assertEqual(max(d.rs_fixed for d in got), 0)

    def test_odd_block_sizes_and_wideband_legs(self):
        self.stream(8000, block=37)
        self.stream(16000)
        self.stream(48000, block=480)


if __name__ == "__main__":
    unittest.main()
