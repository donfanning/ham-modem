"""Two-station tests for the working PACTOR-II/III and Clover-II links."""

import unittest

import numpy as np

from hammodem.keyboard.burst import find_onset, pop_burst
from hammodem.keyboard.clover2 import Clover2Tnc, CloverFrame, Mode
from hammodem.keyboard.clover2_link import Clover2Link
from hammodem.keyboard.pactor2 import Pactor2Modulator
from hammodem.keyboard.pactor2_link import Pactor2Link
from hammodem.keyboard.pactor3_link import Pactor3Link
from hammodem.keyboard.pactor_link import F_DATA, pack_status

N = 800


def _run_pair(a, b, ticks, *, send_at=None, text=b"HELLO", send_b_at=None,
              text_b=b"WORLD", disconnect_at=None, drop=None, n=N):
    """Clock two links against each other, optionally dropping a burst."""
    ra = np.zeros(n, np.int16)
    rb = np.zeros(n, np.int16)
    dropped = 0
    for i in range(ticks):
        ta = a.tick(ra, n)
        tb = b.tick(rb, n)
        if drop is not None and drop[0] <= i < drop[1]:
            ta[:] = 0
            dropped += 1
        ra, rb = tb, ta
        if send_at is not None and i == send_at:
            a.send(text)
        if send_b_at is not None and i == send_b_at:
            b.send(text_b)
        if disconnect_at is not None and i == disconnect_at:
            a.disconnect()
    return dropped


class BurstAcquisitionTest(unittest.TestCase):
    def test_pactor_burst_with_leading_silence(self):
        audio = Pactor2Modulator(level=2).burst(b"A\0B", pack_status(1, 0))
        padded = np.concatenate((np.zeros(160, np.int16), audio))
        onset = find_onset(padded)
        self.assertIsNotNone(onset)
        self.assertLessEqual(abs(onset - 160), 2)
        got = pop_burst(padded, len(audio))
        self.assertIsNotNone(got)
        self.assertEqual(len(got[0]), len(audio))

    def test_clover_burst_with_leading_silence(self):
        audio = Clover2Tnc(mode=Mode.QPSM).transmit(bytes([pack_status(1, 0)]) + b"A\0B")
        padded = np.concatenate((np.zeros(160, np.int16), audio))
        onset = find_onset(padded)
        self.assertEqual(onset, 160)
        got = pop_burst(padded, len(audio))
        self.assertIsNotNone(got)

    def test_incomplete_burst_is_not_consumed(self):
        audio = np.ones(400, np.int16) * 1000
        self.assertIsNone(pop_burst(audio, 800))


class Pactor2LinkTest(unittest.TestCase):
    def test_connect_text_and_disconnect(self):
        events = []
        received = []
        a = Pactor2Link("A", on_event=events.append)
        b = Pactor2Link("B", on_event=events.append, on_text=received.append)
        a.connect("B")
        _run_pair(a, b, 260, send_at=40, disconnect_at=170)
        self.assertIn(b"HELLO", received)
        self.assertEqual(a.state, "idle")
        self.assertEqual(b.state, "idle")
        self.assertTrue(any("connected" in e for e in events))
        self.assertTrue(any("disconnected" in e for e in events))

    def test_loss_retries_and_still_delivers(self):
        received = []
        a = Pactor2Link("A")
        b = Pactor2Link("B", on_text=received.append)
        a.connect("B")
        # Drop the first data burst; the stop-and-wait engine must retransmit.
        dropped = _run_pair(a, b, 320, send_at=40, disconnect_at=240, drop=(42, 55))
        self.assertGreater(dropped, 0)
        self.assertGreaterEqual(a.stats.retries, 1)
        self.assertIn(b"HELLO", received)

    def test_text_in_both_directions(self):
        a_rx, b_rx = [], []
        a = Pactor2Link("A", on_text=a_rx.append)
        b = Pactor2Link("B", on_text=b_rx.append)
        a.connect("B")
        _run_pair(a, b, 220, send_at=40, send_b_at=100, disconnect_at=180)
        self.assertIn(b"HELLO", b_rx)
        self.assertIn(b"WORLD", a_rx)
        self.assertEqual(a.state, "idle")
        self.assertEqual(b.state, "idle")

    def test_terminal_help_and_status(self):
        from hammodem.keyboard.pactor2_link import Pactor2Terminal  # noqa: PLC0415

        out = bytearray()
        term = Pactor2Terminal("A", write=out.extend)
        term.feed_host(b"/help\r")
        term.feed_host(b"/status\r")
        text = bytes(out).decode("latin-1")
        self.assertIn("/connect CALL", text)
        self.assertIn("state=idle", text)


class Pactor3LinkTest(unittest.TestCase):
    def test_connect_text_and_disconnect(self):
        received = []
        a = Pactor3Link("A")
        b = Pactor3Link("B", on_text=received.append)
        a.connect("B")
        _run_pair(a, b, 260, send_at=40, disconnect_at=170)
        self.assertIn(b"HELLO", received)
        self.assertEqual(a.state, "idle")
        self.assertEqual(b.state, "idle")

    def test_text_in_both_directions_and_loss(self):
        a_rx, b_rx = [], []
        a = Pactor3Link("A", on_text=a_rx.append)
        b = Pactor3Link("B", on_text=b_rx.append)
        a.connect("B")
        dropped = _run_pair(a, b, 320, send_at=40, send_b_at=120,
                            disconnect_at=250, drop=(42, 55))
        self.assertGreater(dropped, 0)
        self.assertGreaterEqual(a.stats.retries, 1)
        self.assertIn(b"HELLO", b_rx)
        self.assertIn(b"WORLD", a_rx)
        self.assertEqual(a.state, "idle")
        self.assertEqual(b.state, "idle")


class Clover2LinkTest(unittest.TestCase):
    def test_connect_text_and_disconnect(self):
        received = []
        events = []
        a = Clover2Link("A", mode=Mode.QPSM, on_event=events.append)
        b = Clover2Link("B", mode=Mode.QPSM, on_event=events.append, on_text=received.append)
        a.connect("B")
        _run_pair(a, b, 360, send_at=90, disconnect_at=270, n=1600)
        self.assertIn(b"HELLO", received)
        self.assertEqual(a.state, "idle")
        self.assertEqual(b.state, "idle")
        self.assertTrue(any("connected" in e for e in events))

    def test_text_in_both_directions(self):
        a_rx, b_rx = [], []
        a = Clover2Link("A", mode=Mode.QPSM, on_text=a_rx.append)
        b = Clover2Link("B", mode=Mode.QPSM, on_text=b_rx.append)
        a.connect("B")
        _run_pair(a, b, 900, send_at=90, send_b_at=250, disconnect_at=800, n=1600)
        self.assertIn(b"HELLO", b_rx)
        self.assertIn(b"WORLD", a_rx)
        self.assertEqual(a.state, "idle")
        self.assertEqual(b.state, "idle")


class Clover2ModesTest(unittest.TestCase):
    def test_dual_pulse_modes_round_trip(self):
        for mode in (Mode.PSM2D, Mode.FSM2D):
            with self.subTest(mode=mode):
                tnc = Clover2Tnc(mode=mode)
                audio = tnc.transmit(b"HELLO CLOVER")
                self.assertEqual(tnc.feed(audio), b"HELLO CLOVER")

    def test_ccb_is_28_bytes(self):
        frame = CloverFrame()
        payload = bytes(range(17))
        ccb = frame.encode_ccb(payload)
        self.assertEqual(len(ccb), 28)
        self.assertEqual(frame.decode_ccb(ccb), payload)


if __name__ == "__main__":
    unittest.main()
