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

    def test_errors_and_erasures(self):
        rng = random.Random(4)
        for nroots in (16, 32, 64):
            rs = fx25.ReedSolomon(nroots)
            for n in (255, 96 + nroots):
                data = bytes(rng.randrange(256) for _ in range(n - nroots))
                cw = data + rs.encode(data)
                for e, v in ((nroots, 0), (nroots // 2, nroots // 4), (nroots - 2, 1), (1, nroots // 2 - 1)):
                    bad = bytearray(cw)
                    pos = rng.sample(range(n), e + v)
                    for p in pos:
                        bad[p] ^= rng.randrange(1, 256)
                    era = pos[:e]                                   # we know where e of them are
                    res = rs.decode(bytes(bad), era)
                    self.assertIsNotNone(res, (nroots, n, e, v))
                    self.assertEqual(res[0], cw, (nroots, n, e, v))

    def test_erasures_double_the_reach(self):
        rs = fx25.ReedSolomon(16)
        data = bytes(range(239))
        cw = data + rs.encode(data)
        bad = bytearray(cw)
        for p in range(100, 116):                                  # a 16-byte hole, like a lost packet
            bad[p] ^= 0x5A
        self.assertIsNone(rs.decode(bytes(bad)))                   # 16 unknown errors: beyond 8
        self.assertEqual(rs.decode(bytes(bad), list(range(100, 116)))[0], cw)

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


class Fx25TagErasureTest(unittest.TestCase):
    def test_erased_tag_bits_not_counted_as_errors(self):
        """Tag bits marked erased should not count against the TAG_MAX_ERRORS threshold."""
        rng = random.Random(7)
        frame = ui(50)
        bits = fx25.burst_bits([frame], 4)
        # Flip 6 bits inside the 64-bit tag region and mark them erased.
        tag_offset = 8 * 4    # after 4 lead flags
        bad_pos = rng.sample(range(tag_offset, tag_offset + 64), 6)
        for p in bad_pos:
            bits[p] ^= 1
        erased = [False] * len(bits)
        for p in bad_pos:
            erased[p] = True
        # Without erasures: 6 tag errors > TAG_MAX_ERRORS (4) → frame not found
        rx_no_era = fx25.Fx25Receiver()
        got_no = rx_no_era.feed_bits(bits)
        # With erasures: erased bits masked out → frame found
        rx_era = fx25.Fx25Receiver()
        got_era = rx_era.feed_bits(bits, erased)
        self.assertGreater(len(got_era), 0, "erased-masked tag should be found")
        self.assertEqual(got_era, [frame])

    def test_tag_erasure_masking_does_not_break_clean_frames(self):
        """Erasure-aware tag search delivers clean frames even when no bits are erased."""
        frames_list = [ui(n) for n in (20, 80, 150)]
        bits = fx25.burst_bits(frames_list, 4)
        erased = [False] * len(bits)
        rx = fx25.Fx25Receiver()
        got = rx.feed_bits(bits, erased)
        self.assertEqual(got, frames_list)

    def test_tag_search_rejects_window_with_too_few_known_bits(self):
        frame = ui(50)
        bits = fx25.burst_bits([frame], 4)
        tag_start = 8 * 4
        erased = [False] * len(bits)
        erased[tag_start:tag_start + 33] = [True] * 33
        # At most 31 known tag bits cannot uniquely identify the FX.25 codeword.
        rx = fx25.Fx25Receiver()
        self.assertEqual(rx.feed_bits(bits, erased), [])


class Fx25ModemDedupeTest(unittest.TestCase):
    def test_each_fx25_frame_delivered_once_at_300_bd(self):
        # At 300 bd the FX.25 copy decodes > 1 s after the embedded plain copy; both must collapse to one.
        import numpy as np
        from hammodem.modems import make
        mod, dem = make("afsk300", 8000)
        mod.fx25 = 32
        fs = [ui(40 + i) for i in range(3)]
        audio = np.concatenate([np.concatenate((mod.modulate([f], 0.2), np.zeros(4000, np.int16))) for f in fs])
        got = []
        for i in range(0, len(audio), 160):
            got += dem.feed(audio[i:i + 160])
        self.assertEqual(got, fs)


if __name__ == "__main__":
    unittest.main()
