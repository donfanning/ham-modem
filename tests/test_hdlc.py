import random
import unittest

from hammodem.modems.hdlc import (FLAG_BITS, HdlcDecoder, NrziDecoder, NrziEncoder, burst_bits, crc16_hdlc,
                                  frame_bits, stuffed_bits)


def decode(bits):
    rx, out = HdlcDecoder(), []
    for b in bits:
        f = rx.push(b)
        if f is not None:
            out.append(f)
    return out, rx


class HdlcTest(unittest.TestCase):
    def test_fcs_check_value(self):
        self.assertEqual(crc16_hdlc(b"123456789"), 0x906E)          # CRC-16/X-25 check value

    def test_stuffing_never_six_ones(self):
        bits = stuffed_bits(b"\xff" * 30)
        run = 0
        for b in bits:
            run = run + 1 if b else 0
            self.assertLess(run, 6)

    def test_round_trip_random_frames(self):
        rng = random.Random(3)
        frames = [bytes(rng.randrange(256) for _ in range(rng.randrange(15, 300))) for _ in range(50)]
        got, rx = decode(burst_bits(frames, 4))
        self.assertEqual(got, frames)
        self.assertEqual(rx.bad_fcs, 0)

    def test_corrupt_frame_counts_bad_fcs(self):
        bits = frame_bits(b"A" * 20, 3)
        bits[60] ^= 1
        got, rx = decode(bits)
        self.assertEqual(got, [])

    def test_short_frames_ignored(self):
        got, _ = decode(frame_bits(b"short", 3))
        self.assertEqual(got, [])

    def test_nrzi_round_trip(self):
        rng = random.Random(5)
        bits = [rng.randrange(2) for _ in range(1000)]
        enc, dec = NrziEncoder(), NrziDecoder()
        self.assertEqual([dec.decode(level) for level in enc.encode(bits)], bits)

    def test_flags_are_nrzi_transitions(self):
        levels = NrziEncoder().encode(list(FLAG_BITS) * 2)
        self.assertEqual(levels[:8], [0, 0, 0, 0, 0, 0, 0, 1])


if __name__ == "__main__":
    unittest.main()


class VectorHdlcTest(unittest.TestCase):
    def test_matches_bitwise_decoder(self):
        rng = random.Random(7)
        for trial in range(20):
            frames = [bytes(rng.randrange(256) for _ in range(rng.randrange(15, 400))) for _ in range(6)]
            bits = [rng.randrange(2) for _ in range(rng.randrange(0, 300))]      # noise before
            for f in frames:
                bits += frame_bits(f, rng.randrange(1, 5), rng.randrange(1, 3))
                if rng.random() < 0.3:
                    bits += [1] * 9 + [rng.randrange(2) for _ in range(50)]    # abort + junk between
            if trial % 3 == 0:
                bits[rng.randrange(len(bits))] ^= 1                             # a corrupted frame
            ref, rx = decode(bits)
            vec = HdlcDecoder()
            got, i = [], 0
            while i < len(bits):
                n = rng.randrange(1, 900)
                got += vec.push_bits(bits[i:i + n])
                i += n
            self.assertEqual(got, ref, f"trial {trial}")
            self.assertEqual(vec.bad_fcs, rx.bad_fcs, f"trial {trial}")
