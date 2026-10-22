"""Tests for the PACTOR-I codec, FSK modem, and ARQ station."""

import unittest
import numpy as np

from hammodem.pactor import code, fsk
from hammodem.pactor.arq import PactorStation, MemoryArq


class TestCrc(unittest.TestCase):
    def test_known_crc(self):
        """CRC of empty data with init 0xFFFF is the complement of 0xFFFF."""
        self.assertEqual(code.crc_ccitt(b"", 0xFFFF), 0xFFFF ^ 0xFFFF)  # 0x0000... no
        # Actually ~0xFFFF & 0xFFFF = 0x0000
        self.assertEqual(code.crc_ccitt(b"", 0xFFFF), 0x0000)

    def test_crc_roundtrip(self):
        data = b"HELLO PACTOR"
        crc = code.crc_ccitt(data)
        pkt = data + bytes([crc & 0xFF, crc >> 8])
        self.assertTrue(code.crc_check(pkt))

    def test_crc_detects_corruption(self):
        data = b"HELLO PACTOR"
        crc = code.crc_ccitt(data)
        pkt = bytearray(data + bytes([crc & 0xFF, crc >> 8]))
        pkt[3] ^= 0x42
        self.assertFalse(code.crc_check(bytes(pkt)))

    def test_build_parse_packet(self):
        data = b"TEST DATA HERE!!"[:20]
        pkt = code.build_packet(data, counter=2)
        parsed = code.parse_packet(pkt)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["counter"], 2)
        self.assertFalse(parsed["huffman"])
        self.assertFalse(parsed["qrt"])
        self.assertEqual(parsed["data"], data)

    def test_parse_bad_crc(self):
        data = b"TEST DATA"
        pkt = bytearray(code.build_packet(data, counter=0))
        pkt[-1] ^= 0xFF
        self.assertIsNone(code.parse_packet(bytes(pkt)))


class TestHuffman(unittest.TestCase):
    def test_encode_decode_ascii(self):
        text = b"the quick brown fox"
        packed, nbits = code.huffman_encode(text)
        self.assertGreater(nbits, 0)
        self.assertLess(len(packed), len(text))  # Huffman compresses
        decoded = code.huffman_decode(packed, nbits)
        self.assertEqual(decoded, text)

    def test_single_char(self):
        for ch in b"eE ato":
            packed, nbits = code.huffman_encode(bytes([ch]))
            decoded = code.huffman_decode(packed, nbits)
            self.assertEqual(decoded, bytes([ch]), f"failed for {chr(ch)!r}")

    def test_empty(self):
        packed, nbits = code.huffman_encode(b"")
        self.assertEqual(nbits, 0)
        self.assertEqual(packed, b"")

    def test_all_printable(self):
        text = bytes(range(32, 127))
        packed, nbits = code.huffman_encode(text)
        decoded = code.huffman_decode(packed, nbits)
        self.assertEqual(decoded, text)


class TestControlSignals(unittest.TestCase):
    def test_cs_roundtrip(self):
        for cs in (code.CS1, code.CS2, code.CS3, code.CS4):
            bits = code.cs_bits(cs)
            self.assertEqual(len(bits), 12)
            recovered = code.cs_from_bits(bits)
            self.assertEqual(recovered, cs, f"CS 0x{cs:03X} failed roundtrip")

    def test_hamming_distance(self):
        """All CS pairs have Hamming distance >= 8."""
        signals = [code.CS1, code.CS2, code.CS3, code.CS4]
        for i, a in enumerate(signals):
            for b in signals[i + 1:]:
                dist = bin(a ^ b).count("1")
                self.assertGreaterEqual(dist, 8, f"CS 0x{a:03X} vs 0x{b:03X}: dist {dist}")


class TestStatusByte(unittest.TestCase):
    def test_fields(self):
        s = code.make_status(3, huffman=True, breakin=True, qrt=False)
        self.assertEqual(s & 0x03, 3)
        self.assertTrue(s & code.STAT_HUFFMAN)
        self.assertTrue(s & code.STAT_BREAKIN)
        self.assertFalse(s & code.STAT_QRT)

    def test_qrt(self):
        s = code.make_status(0, qrt=True)
        self.assertTrue(s & code.STAT_QRT)


class TestCallsign(unittest.TestCase):
    def test_encode_decode(self):
        self.assertEqual(code.decode_call(code.encode_call("W1AW")), "W1AW")
        self.assertEqual(code.decode_call(code.encode_call("DL6MAA")), "DL6MAA")

    def test_padding(self):
        raw = code.encode_call("AB")
        self.assertEqual(len(raw), 8)
        self.assertEqual(raw[2:], bytes([0x0F] * 6))


class TestFsk(unittest.TestCase):
    def test_modulate_demodulate_200(self):
        """Round-trip: mod 200 bd -> demod -> slice should recover bits."""
        bits_in = [1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0, 0]
        mod = fsk.Modulator()
        audio = mod.bits(bits_in, baud=200)
        self.assertEqual(len(audio), len(bits_in) * fsk.spb(200))
        disc = fsk.Discriminator(baud=200)
        d, e = disc.process(audio.astype(float))
        phase, score = fsk.best_phase(d, 0, len(bits_in), baud=200)
        bits_out, soft = fsk.slice_bits(d, phase, len(bits_in), baud=200)
        self.assertEqual(bits_out, bits_in)

    def test_modulate_demodulate_100(self):
        bits_in = [1, 0, 1, 1, 0, 0, 1, 0]
        mod = fsk.Modulator()
        audio = mod.bits(bits_in, baud=100)
        self.assertEqual(len(audio), len(bits_in) * fsk.spb(100))
        disc = fsk.Discriminator(baud=100)
        d, e = disc.process(audio.astype(float))
        phase, score = fsk.best_phase(d, 0, len(bits_in), baud=100)
        bits_out, soft = fsk.slice_bits(d, phase, len(bits_in), baud=100)
        self.assertEqual(bits_out, bits_in)

    def test_packet_roundtrip_200(self):
        """Encode a full 200 bd PACTOR packet, modulate, demodulate, parse."""
        data = b"HELLO PACTOR TEST!!"[:20]
        pkt = code.build_packet(data, counter=1)
        hdr = code.HEADER1
        hdr_bits = [(hdr >> i) & 1 for i in range(8)]
        pkt_bits = []
        for byte in pkt:
            for i in range(8):
                pkt_bits.append((byte >> i) & 1)
        all_bits = hdr_bits + pkt_bits
        mod = fsk.Modulator()
        audio = mod.bits(all_bits, baud=200)
        # add some silence before and after
        audio = np.concatenate((np.zeros(200, np.int16), audio, np.zeros(200, np.int16)))
        disc = fsk.Discriminator(baud=200)
        d, e = disc.process(audio.astype(float))
        # find the burst
        start = fsk.detect_burst(e, threshold=0.001, min_samples=100)
        self.assertIsNotNone(start)
        phase, _ = fsk.best_phase(d, start, len(all_bits), baud=200)
        bits_out, _ = fsk.slice_bits(d, start + phase, len(all_bits), baud=200)
        self.assertEqual(len(bits_out), len(all_bits))
        # recover the header
        hdr_out = sum(bits_out[i] << i for i in range(8))
        self.assertEqual(hdr_out, hdr)
        # recover the packet
        pkt_out = bytearray()
        for byte_i in range(len(pkt)):
            val = 0
            for bit_i in range(8):
                val |= bits_out[8 + byte_i * 8 + bit_i] << bit_i
            pkt_out.append(val)
        parsed = code.parse_packet(bytes(pkt_out))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["counter"], 1)
        self.assertEqual(parsed["data"], data)

    def test_packet_roundtrip_100(self):
        """Encode a full 100 bd PACTOR packet, modulate, demodulate, parse."""
        data = b"HI THERE"[:8]
        pkt = code.build_packet(data, counter=0)
        hdr = code.HEADER2
        hdr_bits = [(hdr >> i) & 1 for i in range(8)]
        pkt_bits = []
        for byte in pkt:
            for i in range(8):
                pkt_bits.append((byte >> i) & 1)
        all_bits = hdr_bits + pkt_bits
        mod = fsk.Modulator()
        audio = mod.bits(all_bits, baud=100)
        audio = np.concatenate((np.zeros(200, np.int16), audio, np.zeros(200, np.int16)))
        disc = fsk.Discriminator(baud=100)
        d, e = disc.process(audio.astype(float))
        start = fsk.detect_burst(e, threshold=0.001, min_samples=100)
        self.assertIsNotNone(start)
        phase, _ = fsk.best_phase(d, start, len(all_bits), baud=100)
        bits_out, _ = fsk.slice_bits(d, start + phase, len(all_bits), baud=100)
        hdr_out = sum(bits_out[i] << i for i in range(8))
        self.assertEqual(hdr_out, hdr)
        pkt_out = bytearray()
        for byte_i in range(len(pkt)):
            val = 0
            for bit_i in range(8):
                val |= bits_out[8 + byte_i * 8 + bit_i] << bit_i
            pkt_out.append(val)
        parsed = code.parse_packet(bytes(pkt_out))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["counter"], 0)
        self.assertEqual(parsed["data"], data)

    def test_cs_roundtrip_audio(self):
        """Modulate and demodulate a control signal through audio."""
        for cs in (code.CS1, code.CS2, code.CS3, code.CS4):
            mod = fsk.Modulator()
            audio = mod.bits(code.cs_bits(cs), baud=100)
            audio = np.concatenate((np.zeros(100, np.int16), audio, np.zeros(100, np.int16)))
            disc = fsk.Discriminator(baud=100)
            d, _ = disc.process(audio.astype(float))
            phase, _ = fsk.best_phase(d, 100, 12, baud=100)
            bits_out, _ = fsk.slice_bits(d, 100 + phase, 12, baud=100)
            recovered = code.cs_from_bits(bits_out)
            self.assertEqual(recovered, cs, f"CS 0x{cs:03X} failed audio roundtrip")


class TestMemoryArq(unittest.TestCase):
    def test_accumulation(self):
        marq = MemoryArq(4)
        marq.add([1.0, -0.5, 0.3, -1.0])
        marq.add([0.5, -0.3, -0.1, -0.5])
        bits = marq.decide()
        self.assertEqual(bits, [1, 0, 1, 0])  # 1.5 > 0, -0.8 < 0, 0.2 > 0, -1.5 < 0

    def test_noisy_recovery(self):
        """Noisy soft values from two tries should converge on the correct bits."""
        true_bits = [1, 0, 1, 1, 0, 0, 1, 0]
        marq = MemoryArq(8)
        rng = np.random.RandomState(42)
        for _ in range(3):
            soft = [(1.0 if b else -1.0) + rng.normal(0, 0.8) for b in true_bits]
            marq.add(soft)
        decided = marq.decide()
        self.assertEqual(decided, true_bits)


class TestNoiseRobustness(unittest.TestCase):
    def test_200bd_with_noise(self):
        """200 bd packet survives moderate AWGN."""
        data = b"NOISE TEST HERE!!!!"[:20]
        pkt = code.build_packet(data, counter=2)
        hdr = code.HEADER1
        hdr_bits = [(hdr >> i) & 1 for i in range(8)]
        pkt_bits = []
        for byte in pkt:
            for i in range(8):
                pkt_bits.append((byte >> i) & 1)
        all_bits = hdr_bits + pkt_bits
        mod = fsk.Modulator()
        audio = mod.bits(all_bits, baud=200).astype(float)
        rng = np.random.RandomState(123)
        snr_db = 15
        noise_power = 10 ** (-snr_db / 20) * np.std(audio)
        noisy = (audio + rng.normal(0, noise_power, len(audio))).astype(np.int16)
        noisy = np.concatenate((np.zeros(200, np.int16), noisy, np.zeros(200, np.int16)))
        disc = fsk.Discriminator(baud=200)
        d, e = disc.process(noisy.astype(float))
        start = fsk.detect_burst(e, threshold=0.0005, min_samples=80)
        self.assertIsNotNone(start, "burst not detected at 15 dB SNR")
        phase, _ = fsk.best_phase(d, start, len(all_bits), baud=200)
        bits_out, _ = fsk.slice_bits(d, start + phase, len(all_bits), baud=200)
        pkt_out = bytearray()
        for byte_i in range(len(pkt)):
            val = 0
            for bit_i in range(8):
                val |= bits_out[8 + byte_i * 8 + bit_i] << bit_i
            pkt_out.append(val)
        parsed = code.parse_packet(bytes(pkt_out))
        self.assertIsNotNone(parsed, "CRC failed at 15 dB SNR")
        self.assertEqual(parsed["data"], data)


class TestHuffmanInPacket(unittest.TestCase):
    def test_huffman_packet(self):
        """Build a packet with Huffman-compressed text and verify the text survives."""
        text = b"the quick brown fox"
        packed, nbits = code.huffman_encode(text)
        self.assertLessEqual(len(packed), 20)
        padded = packed + bytes(20 - len(packed))
        pkt = code.build_packet(padded, counter=0, huffman=True)
        parsed = code.parse_packet(pkt)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["huffman"])
        decoded = code.huffman_decode(parsed["data"], nbits)
        self.assertEqual(decoded, text)


if __name__ == "__main__":
    unittest.main()
