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

    def test_erased_sync_bits_are_ignored_but_known_bits_still_gate_sync(self):
        f = Frame.ui("N0CALL", "APRS", "sync erasure").encode()
        bits = il2p.burst_levels([f], 4, max_fec=True)
        start = 4 * 8
        erased = [False] * len(bits)
        for offset in range(6):
            bits[start + offset] ^= 1
            erased[start + offset] = True

        no_era = il2p.Il2pReceiver()
        self.assertEqual(no_era.feed_bits(bits + [0, 1] * 8), [])
        with_era = il2p.Il2pReceiver()
        got = with_era.feed_bits(bits + [0, 1] * 8, erased + [False] * 16)
        self.assertEqual(got, [f])

    def test_sync_with_too_few_known_bits_is_rejected(self):
        bits = il2p.burst_levels([frames()[0]], 4, max_fec=True)
        start = 4 * 8
        erased = [False] * len(bits)
        erased[start:start + 9] = [True] * 9
        # Nine erased sync bits leave only fifteen known bits, below the 16-bit gate.
        r = il2p.Il2pReceiver()
        self.assertEqual(r.feed_bits(bits + [0, 1] * 8, erased + [False] * 16), [])


class Il2pcCrcTest(unittest.TestCase):
    def test_spec_example_crc_bytes(self):
        """CRC of the IL2P spec v0.6 S-frame example must match the published bytes 7F 00 1D 2B."""
        raw_no_fcs = bytes.fromhex("96826488 8AAE E496 9668 908A 946F 81".replace(" ", ""))
        crc = il2p.encode_crc(raw_no_fcs)
        self.assertEqual(list(crc), [0x7F, 0x00, 0x1D, 0x2B])

    def test_verify_crc_accepts_correct_bytes(self):
        raw_no_fcs = bytes.fromhex("96826488 8AAE E496 9668 908A 946F 81".replace(" ", ""))
        self.assertTrue(il2p.verify_crc(raw_no_fcs, bytes([0x7F, 0x00, 0x1D, 0x2B])))

    def test_verify_crc_rejects_wrong_bytes(self):
        raw_no_fcs = bytes.fromhex("96826488 8AAE E496 9668 908A 946F 81".replace(" ", ""))
        # bytes([0x00]*4) decodes to CRC 0x0000, which ≠ 0xF0DB
        self.assertFalse(il2p.verify_crc(raw_no_fcs, bytes(4)))

    def test_il2pc_roundtrip_frames(self):
        """Frames encoded with crc=True decode via Il2pReceiver(crc_mode=True)."""
        fs = frames()
        bits = il2p.burst_levels(fs, 4, crc=True)
        r = il2p.Il2pReceiver(crc_mode=True)
        got = r.feed_bits(bits + [0, 1] * 8)
        self.assertEqual(got, fs)

    def test_il2pc_bad_crc_rejected(self):
        """A frame with a corrupted CRC trailer is silently dropped."""
        f = Frame.ui("N0CALL", "APRS", ">test crc rejection").encode()
        encoded = il2p.encode_frame(f, crc=True)
        # Flip all 4 CRC bytes so every nibble is wrong.
        bad = bytearray(encoded)
        for i in range(1, 5):
            bad[-i] ^= 0x55
        bits = il2p._bits_msb(bytes([il2p.PREAMBLE] * 4) + bytes(bad) + bytes([il2p.PREAMBLE]))
        r = il2p.Il2pReceiver(crc_mode=True)
        got = r.feed_bits(bits + [0, 1] * 8)
        self.assertEqual(got, [])

    def test_plain_il2p_unaffected_by_crc_flag(self):
        """Frames encoded without CRC still decode via crc_mode=False receiver."""
        fs = frames()  # uses all 6; the SABM frame has no payload and needs later frames' bits to flush search
        bits = il2p.burst_levels(fs, 4, crc=False)
        r = il2p.Il2pReceiver(crc_mode=False)
        got = r.feed_bits(bits + [0, 1] * 8)
        self.assertEqual(got, fs)


if __name__ == "__main__":
    unittest.main()
