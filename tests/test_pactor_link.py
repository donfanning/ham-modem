"""Regression tests for the shared local burst ARQ and PACTOR soft combining."""

import unittest
from unittest.mock import patch

import numpy as np

from hammodem.keyboard.pactor_link import (
    CS_ACK,
    CS_NACK,
    F_DATA,
    LinkBase,
    pack_status,
)


class FakeLink(LinkBase):
    def __init__(self, on_text):
        super().__init__("N0CALL", on_text=on_text)
        self.frames = []
        self.controls = []

    def decode_data(self, audio):
        return self.frames.pop(0)

    def make_control(self, kind, seq=0, arg=0):
        self.controls.append((kind, seq, arg))
        return np.ones(1, dtype=np.int16)


class ReceiveSequenceTest(unittest.TestCase):
    def setUp(self):
        self.delivered = []
        self.link = FakeLink(self.delivered.append)
        self.link.state = "connected"
        self.link.role = "irs"
        self.audio = np.ones(1, dtype=np.int16)

    def _frame(self, text, seq):
        return {"status": pack_status(F_DATA, seq), "data": text}

    def test_retransmit_after_lost_ack_is_acked_but_delivered_once(self):
        self.link.frames = [self._frame(b"hello", 0) for _ in range(3)]

        # Later copies represent repeated retries after ACK loss. Every copy
        # is ACKed, but none is delivered after the first sequence advance.
        for _ in range(3):
            self.link._handle_data(self.audio)

        self.assertEqual(self.delivered, [b"hello"])
        self.assertEqual(self.link.controls, [(CS_ACK, 0, 0)] * 3)
        self.assertEqual(self.link._rx_seq, 1)

    def test_receive_sequence_wraps_after_new_frame(self):
        self.link.frames = [
            self._frame(b"first", 0),
            self._frame(b"second", 1),
            self._frame(b"third", 0),
        ]
        for _ in range(3):
            self.link._handle_data(self.audio)

        self.assertEqual(self.delivered, [b"first", b"second", b"third"])
        self.assertEqual([seq for _, seq, _ in self.link.controls], [0, 1, 0])

    def test_bad_frame_nacks_expected_receive_sequence(self):
        self.link._seq = 1  # Local transmit sequence is independent of receive sequence.
        self.link._rx_seq = 0
        self.link.frames = [None]

        self.link._handle_data(self.audio)

        self.assertEqual(self.link.controls, [(CS_NACK, 0, 0)])


class _MemSpy:
    def __init__(self):
        self.added = 0
        self.reset_count = 0

    def add(self, *args):
        self.added += 1

    def try_decode(self):
        return None

    def reset(self):
        self.reset_count += 1


class PactorMemoryResetTest(unittest.TestCase):
    def _make_link(self, which):
        if which == 2:
            from hammodem.keyboard import pactor2_link as module
            link = module.Pactor2Link("N0CALL")
        else:
            from hammodem.keyboard import pactor3_link as module
            link = module.Pactor3Link("N0CALL")
        link.state = "connected"
        link.role = "irs"
        link._mem = _MemSpy()
        return module, link

    def test_successful_pactor2_data_decode_resets_combining_history(self):
        module, link = self._make_link(2)
        frame = {"status": pack_status(F_DATA, 0), "data": b"payload"}
        link.demod.feed_burst = lambda audio: frame

        with patch.object(module, "dpsk_demodulate_soft", return_value=np.ones(182)):
            result = link.decode_data(np.ones(64, dtype=np.int16))

        self.assertEqual(result, frame)
        self.assertEqual(link._mem.added, 1)
        self.assertEqual(link._mem.reset_count, 1)

    def test_successful_pactor3_data_decode_resets_combining_history(self):
        module, link = self._make_link(3)
        frame = {"status": pack_status(F_DATA, 0), "data": b"payload"}
        link.demod.feed_burst = lambda audio: frame

        with patch.object(module, "dpsk_demodulate_soft", return_value=np.ones(91)):
            result = link.decode_data(np.ones(64, dtype=np.int16))

        self.assertEqual(result, frame)
        self.assertEqual(link._mem.added, 1)
        self.assertEqual(link._mem.reset_count, 1)

    def test_failed_decode_keeps_soft_bits_for_retry_combining(self):
        for mode in (2, 3):
            with self.subTest(mode=mode):
                module, link = self._make_link(mode)
                link.demod.feed_burst = lambda audio: None
                with patch.object(module, "dpsk_demodulate_soft", return_value=np.ones(182)):
                    result = link.decode_data(np.ones(64, dtype=np.int16))
                self.assertIsNone(result)
                self.assertEqual(link._mem.added, 1)
                self.assertEqual(link._mem.reset_count, 0)


if __name__ == "__main__":
    unittest.main()
