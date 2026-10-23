import unittest
import time

from hammodem.keyboard.stanag5066 import (
    crc32_5066, encode_address, decode_address,
    DPdu, DPduType, DataLinkSession, SYNC_WORD, HEADER_SIZE,
    segment, reassemble, ServiceType, SPrimitive,
)


class CrcTest(unittest.TestCase):
    def test_empty(self):
        c = crc32_5066(b"")
        self.assertIsInstance(c, int)

    def test_deterministic(self):
        self.assertEqual(crc32_5066(b"hello"), crc32_5066(b"hello"))

    def test_different_inputs_differ(self):
        self.assertNotEqual(crc32_5066(b"abc"), crc32_5066(b"abd"))

    def test_known_value(self):
        c = crc32_5066(b"\x00" * 8)
        self.assertIsInstance(c, int)
        self.assertGreaterEqual(c, 0)
        self.assertLess(c, 2**32)


class AddressTest(unittest.TestCase):
    def test_round_trip(self):
        call = "N0CALL"
        raw = encode_address(call)
        self.assertEqual(len(raw), 7)
        got = decode_address(raw)
        self.assertTrue(got.startswith("N0CALL"))

    def test_padding(self):
        raw = encode_address("AB")
        got = decode_address(raw)
        self.assertTrue(got.startswith("AB"))

    def test_max_length(self):
        raw = encode_address("ABCDEFGHIJKLMN")
        self.assertEqual(len(raw), 7)

    def test_numeric(self):
        raw = encode_address("1234")
        got = decode_address(raw)
        self.assertTrue(got.startswith("1234"))


class DPduEncodeDecodeTest(unittest.TestCase):
    def test_data_only_round_trip(self):
        pdu = DPdu(pdu_type=DPduType.DATA_ONLY, src=encode_address("N0CALL"),
                   size=5, tx_seq=42, data=b"HELLO")
        raw = pdu.encode()
        self.assertTrue(raw.startswith(SYNC_WORD))
        self.assertGreaterEqual(len(raw), HEADER_SIZE + 5)
        decoded = DPdu.decode(raw)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.pdu_type, DPduType.DATA_ONLY)
        self.assertEqual(decoded.tx_seq, 42)
        self.assertEqual(decoded.data, b"HELLO")
        self.assertEqual(decoded.size, 5)

    def test_ack_round_trip(self):
        pdu = DPdu(pdu_type=DPduType.ACK_ONLY, rx_seq=10)
        raw = pdu.encode()
        decoded = DPdu.decode(raw)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.pdu_type, DPduType.ACK_ONLY)
        self.assertEqual(decoded.rx_seq, 10)

    def test_non_arq_round_trip(self):
        pdu = DPdu(pdu_type=DPduType.NON_ARQ_DATA, data=b"BROADCAST",
                   size=9, src=encode_address("W1AW"))
        raw = pdu.encode()
        decoded = DPdu.decode(raw)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.pdu_type, DPduType.NON_ARQ_DATA)
        self.assertEqual(decoded.data, b"BROADCAST")

    def test_eot_flag(self):
        pdu = DPdu(pdu_type=DPduType.DATA_ONLY, eot=True, size=3, data=b"END")
        raw = pdu.encode()
        decoded = DPdu.decode(raw)
        self.assertTrue(decoded.eot)

    def test_corrupt_crc_rejected(self):
        pdu = DPdu(pdu_type=DPduType.DATA_ONLY, size=3, data=b"BAD")
        raw = bytearray(pdu.encode())
        raw[-1] ^= 0xFF
        self.assertIsNone(DPdu.decode(bytes(raw)))

    def test_short_rejected(self):
        self.assertIsNone(DPdu.decode(b"\x90\xEB\x00"))

    def test_bad_sync_rejected(self):
        pdu = DPdu(pdu_type=DPduType.DATA_ONLY, size=2, data=b"NO")
        raw = bytearray(pdu.encode())
        raw[0] = 0x00
        self.assertIsNone(DPdu.decode(bytes(raw)))

    def test_empty_data(self):
        pdu = DPdu(pdu_type=DPduType.DATA_ONLY, size=0, data=b"")
        raw = pdu.encode()
        decoded = DPdu.decode(raw)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.data, b"")

    def test_max_seq(self):
        pdu = DPdu(pdu_type=DPduType.DATA_ONLY, tx_seq=255, size=1, data=b"X")
        raw = pdu.encode()
        decoded = DPdu.decode(raw)
        self.assertEqual(decoded.tx_seq, 255)


class SegmentationTest(unittest.TestCase):
    def test_small_message(self):
        segs = segment(b"hello", 1023)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0], b"hello")

    def test_exact_split(self):
        data = b"A" * 20
        segs = segment(data, 10)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0], b"A" * 10)
        self.assertEqual(segs[1], b"A" * 10)

    def test_uneven_split(self):
        data = b"B" * 25
        segs = segment(data, 10)
        self.assertEqual(len(segs), 3)
        self.assertEqual(len(segs[2]), 5)

    def test_reassemble(self):
        segs = {0: b"AA", 1: b"BB", 2: b"CC"}
        self.assertEqual(reassemble(segs, 3), b"AABBCC")

    def test_empty(self):
        segs = segment(b"")
        self.assertEqual(len(segs), 1)


class SessionArqTest(unittest.TestCase):
    def test_single_segment_transfer(self):
        a = DataLinkSession(local_addr="N0CALL")
        b = DataLinkSession(local_addr="K1ABC")
        a.bind("K1ABC")
        b.bind("N0CALL")
        a.send(b"Hello ARQ")
        a.pump()
        self.assertEqual(len(a.outgoing_pdus), 1)
        b.receive(a.outgoing_pdus.pop(0))
        self.assertEqual(len(b.incoming_data), 1)
        self.assertEqual(b.incoming_data[0], b"Hello ARQ")
        self.assertEqual(len(b.outgoing_pdus), 1)
        a.receive(b.outgoing_pdus.pop(0))
        self.assertEqual(a.pending_acks(), 0)

    def test_multi_segment_transfer(self):
        a = DataLinkSession(local_addr="N0CALL")
        b = DataLinkSession(local_addr="K1ABC")
        a.bind("K1ABC")
        b.bind("N0CALL")
        msg = b"X" * 2000
        a.send(msg)
        a.pump()
        self.assertGreater(len(a.outgoing_pdus), 1)
        received = bytearray()
        while a.outgoing_pdus:
            b.receive(a.outgoing_pdus.pop(0))
        for chunk in b.incoming_data:
            received.extend(chunk)
        self.assertEqual(bytes(received), msg)

    def test_non_arq_delivery(self):
        a = DataLinkSession(local_addr="N0CALL")
        b = DataLinkSession(local_addr="K1ABC")
        a.bind("K1ABC")
        a.send_non_arq(b"Broadcast msg")
        self.assertGreater(len(a.outgoing_pdus), 0)
        b.receive(a.outgoing_pdus.pop(0))
        self.assertEqual(b.incoming_data[0], b"Broadcast msg")

    def test_ack_clears_window(self):
        a = DataLinkSession(local_addr="N0CALL")
        b = DataLinkSession(local_addr="K1ABC")
        a.bind("K1ABC")
        b.bind("N0CALL")
        a.send(b"Data")
        a.pump()
        self.assertEqual(a.pending_acks(), 1)
        b.receive(a.outgoing_pdus.pop(0))
        a.receive(b.outgoing_pdus.pop(0))
        self.assertEqual(a.pending_acks(), 0)

    def test_bind_unbind(self):
        s = DataLinkSession(local_addr="N0CALL")
        self.assertFalse(s.connected)
        s.bind("K1ABC")
        self.assertTrue(s.connected)
        self.assertEqual(s.remote_addr, "K1ABC")
        s.unbind()
        self.assertFalse(s.connected)


class SPrimitiveTest(unittest.TestCase):
    def test_fields(self):
        p = SPrimitive(kind="DATA", src="N0CALL", dst="K1ABC", data=b"test",
                       service=ServiceType.ARQ, priority=3)
        self.assertEqual(p.kind, "DATA")
        self.assertEqual(p.service, ServiceType.ARQ)
        self.assertEqual(p.priority, 3)


if __name__ == "__main__":
    unittest.main()
