import random
import unittest

from hammodem import ax25, il2p
from hammodem.ax25 import Address, Frame


def frames():
    out = [Frame.ui("N0CALL-3", "APRS", ">il2p type 1 ui").encode(),
           Frame.ui("N0CALL", "APRS", ">via digis -> type 0", "WIDE1-1,WIDE2-1").encode(),
           Frame(Address("K1ABC"), Address("N0CALL"), [], ax25.SABM | ax25.PF, None, b"").encode(),
           Frame(Address("K1ABC", 2), Address("N0CALL", 1), [], (3 << 5) | (5 << 1), 0xF0, b"I-frame data").encode(),
           Frame(Address("N0CALL"), Address("K1ABC"), [], ax25.RR | (4 << 5) | ax25.PF, None, b"",
                 command=False).encode(),
           Frame.ui("N0CALL", "CQ", "x" * 600).encode()]                  # several payload blocks
    return out


def rx_bits(bits):
    r = il2p.Il2pReceiver()
    return r.feed_bits(bits + [0, 1] * 8), r                              # trailing bits flush the last decode


class Il2pTest(unittest.TestCase):
    def test_scrambler_round_trip(self):
        rng = random.Random(1)
        for n in (1, 13, 100, 239):
            data = bytes(rng.randrange(256) for _ in range(n))
            self.assertEqual(il2p.descramble_block(il2p.scramble_block(data)), data)

    def test_block_plan(self):
        self.assertEqual(il2p.payload_blocks(100, True), ([100], 16))
        self.assertEqual(il2p.payload_blocks(600, True), ([200, 200, 200], 16))
        self.assertEqual(il2p.payload_blocks(500, False), ([167, 167, 166], 6))

    def test_round_trip_all_kinds(self):
        for max_fec in (True, False):
            fs = frames()
            got, _ = rx_bits(il2p.burst_levels(fs, 4, max_fec=max_fec))
            self.assertEqual(got, fs, f"max_fec={max_fec}")

    def test_inverted_polarity(self):
        fs = frames()[:2]
        got, _ = rx_bits(il2p.burst_levels(fs, 4, invert=True))
        self.assertEqual(got, fs)

    def test_repairs_errors(self):
        f = Frame.ui("N0CALL", "APRS", "y" * 200).encode()
        bits = il2p.burst_levels([f], 4)
        rng = random.Random(2)
        start = 8 * (4 + 3 + 15 + 20)                                    # into the payload
        for i in rng.sample(range(start, start + 8 * 150), 7):          # scattered bit errors (<= 8 bytes)
            bits[i] ^= 1
        bits[8 * 4 + 5] ^= 1                                              # one bad bit in the sync word
        got, r = rx_bits(bits)
        self.assertEqual(got, [f])
        self.assertGreater(r.corrected, 0)

    def test_erasure_aware_decoding_repairs_more(self):
        """Erasure-aware RS repair: marking damaged bytes as erased doubles the correctable count."""
        import numpy as np
        f = Frame.ui("N0CALL", "APRS", "z" * 100).encode()
        bits = il2p.burst_levels([f], 4, max_fec=True)
        # Corrupt 9 consecutive bytes in the payload (> 8: beyond errors-only limit, within erasures limit).
        byte_start = 4 + 3 + 15   # preamble + sync + header
        bit_start = byte_start * 8
        erased = [False] * len(bits)
        for b in range(9):
            for bit in range(8):
                bits[bit_start + b * 8 + bit] ^= 1
                erased[bit_start + b * 8 + bit] = True
        # Without erasures → repair fails
        got_no_era, _ = rx_bits(bits)
        self.assertEqual(got_no_era, [])
        # With erasures → repair succeeds
        r = il2p.Il2pReceiver()
        got_era = r.feed_bits(bits + [0, 1] * 8, erased + [False] * 16)
        self.assertEqual(got_era, [f])
        self.assertGreater(r.erasures, 0)

    def test_erasure_flag_propagates_to_counter(self):
        """feed_bits with erased bits increments self.erasures."""
        import numpy as np
        f = Frame.ui("K1ABC", "APRS", "hello").encode()
        bits = il2p.burst_levels([f], 4, max_fec=True)
        # Mark a few payload bits erased without actually corrupting them.
        erased = [False] * len(bits)
        byte_offset = (4 + 3 + 15) * 8   # into payload
        for i in range(8):
            erased[byte_offset + i] = True
        r = il2p.Il2pReceiver()
        got = r.feed_bits(bits + [0, 1] * 8, erased + [False] * 16)
        self.assertEqual(got, [f])                  # still decoded despite erased flag
        self.assertGreater(r.erasures, 0)


if __name__ == "__main__":
    unittest.main()
