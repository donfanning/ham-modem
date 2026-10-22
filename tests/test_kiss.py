import unittest

from hammodem import kiss


class KissTest(unittest.TestCase):
    def test_escape_round_trip(self):
        data = bytes((0xC0, 0xDB, 0x01, 0xDC, 0xDD, 0xC0))
        wire = kiss.encode(data, port=2)
        self.assertNotIn(0xC0, wire[1:-1])
        self.assertEqual(kiss.Decoder().feed(wire), [(2, kiss.DATA, data)])

    def test_byte_at_a_time_and_parameters(self):
        wire = b"junk" + kiss.encode(bytes((30,)), command=kiss.TXDELAY) + kiss.encode(b"abc") + bytes((0xC0, 0xFF, 0xC0))
        dec, out = kiss.Decoder(), []
        for b in wire:
            out += dec.feed(bytes((b,)))
        self.assertEqual(out, [(0, kiss.TXDELAY, bytes((30,))), (0, kiss.DATA, b"abc"), (15, kiss.RETURN, b"")])

    def test_back_to_back_fends(self):
        self.assertEqual(kiss.Decoder().feed(b"\xc0\xc0\xc0\x00x\xc0"), [(0, 0, b"x")])


if __name__ == "__main__":
    unittest.main()
