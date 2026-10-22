import random
import unittest

from hammodem import fx25
from hammodem.ax25 import Frame
from hammodem.modems.hdlc import HdlcDecoder


def ui(n):
    return Frame.ui("N0CALL", "APRS", "x" * n).encode()


class ReedSolomonTest(unittest.TestCase):
    def test_corrects_up_to_half_nroots(self):
        rng = random.Random(1)
        for nroots in (16, 32, 64):
            rs = fx25.ReedSolomon(nroots)
            for n in (255, 128, 48 + nroots):
                k = n - nroots
                data = bytes(rng.randrange(256) for _ in range(k))
                cw = data + rs.encode(data)
                self.assertEqual(rs.decode(cw), (cw, 0))
                for errors in (1, nroots // 4, nroots // 2):
                    bad = bytearray(cw)
                    for p in rng.sample(range(n), errors):
                        bad[p] ^= rng.randrange(1, 256)
                    self.assertEqual(rs.decode(bytes(bad)), (cw, errors), (nroots, n, errors))

    def test_detects_too_many_errors(self):
        rng = random.Random(2)
        rs = fx25.ReedSolomon(16)
        data = bytes(rng.randrange(256) for _ in range(239))
        cw = data + rs.encode(data)
        for _ in range(20):
            bad = bytearray(cw)
            for p in rng.sample(range(255), 12):                    # beyond the 8 it can fix
                bad[p] ^= rng.randrange(1, 256)
            res = rs.decode(bytes(bad))
            self.assertTrue(res is None or res[0] != cw)            # never claims the original back


class Fx25FramingTest(unittest.TestCase):
    def test_choose_smallest_code(self):
        self.assertEqual(fx25.choose_tag(30, 16), 0x04)            # RS(48,32)
        self.assertEqual(fx25.choose_tag(100, 32), 0x06)           # RS(160,128)
        self.assertIsNone(fx25.choose_tag(240, 16))

    def test_plain_hdlc_receiver_still_decodes(self):
        frame = ui(60)
        bits = fx25.burst_bits([frame], 4)
        rx, got = HdlcDecoder(), []
        for b in bits:
            f = rx.push(b)
            if f is not None:
                got.append(f)
        self.assertEqual(got, [frame])                             # backward compatible

    def test_round_trip_and_repair(self):
        rng = random.Random(3)
        for nroots in (16, 32, 64):
            frames = [ui(rng.randrange(10, 150)) for _ in range(5)]
            bad, lead = [], 8
            bits = fx25.burst_bits([], lead)[:8 * lead]
            for f in frames:
                cw = fx25.encode_bits(f, nroots)
                # Corrupt nroots/2 whole bytes inside the codeword (a lost RTP packet's worth of garbage).
                start = 64 + 8 * rng.randrange(0, 4)
                for byte in range(nroots // 2):
                    for j in range(8):
                        cw[start + 8 * byte + j] ^= rng.randrange(2)
                cw[start] ^= 1                                       # make sure the first byte really changes
                bits += cw + [0, 1, 1, 1, 1, 1, 1, 0]
            rx = fx25.Fx25Receiver()
            got = []
            for i in range(0, len(bits), 333):                       # arbitrary block boundaries
                got += rx.feed_bits(bits[i:i + 333])
            self.assertEqual(got, frames, nroots)
            self.assertEqual(rx.corrected, len(frames))

    def test_tag_with_bit_errors_still_found(self):
        frame = ui(40)
        bits = fx25.burst_bits([frame], 4)
        bits[32 + 3] ^= 1                                           # damage the tag (after 4 flags)
        bits[32 + 40] ^= 1
        self.assertEqual(fx25.Fx25Receiver().feed_bits(bits), [frame])

    def test_long_frame_falls_back_to_plain(self):
        self.assertIsNone(fx25.encode_bits(ui(250), 16))


if __name__ == "__main__":
    unittest.main()
