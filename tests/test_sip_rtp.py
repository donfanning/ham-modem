import select
import time
import unittest

import numpy as np

from hammodem.audio import g711
from hammodem.audio.rtp import L16_16K, PCMA, PCMU, RtpSession
from hammodem.audio.sip import SipLeg, build_sdp, match_codec, parse_sdp


class G711Test(unittest.TestCase):
    def test_round_trip_error_small(self):
        x = (np.sin(np.arange(800) * 0.3) * 16000).astype(np.int16)
        for law in ("pcmu", "pcma"):
            y = g711.decode(g711.encode(x, law), law)
            self.assertLess(np.max(np.abs(y - x)), 600)


class SdpTest(unittest.TestCase):
    def test_offer_answer(self):
        sdp = build_sdp("10.0.0.1", 4000, [PCMU, PCMA, L16_16K])
        remote, offered = parse_sdp(sdp)
        self.assertEqual(remote, ("10.0.0.1", 4000))
        self.assertEqual(offered, [(0, "PCMU", 8000), (8, "PCMA", 8000), (96, "L16", 16000)])
        self.assertEqual(match_codec(offered, [PCMA]).pt, 8)
        self.assertEqual(match_codec([(118, "L16", 16000)], [L16_16K]).pt, 118)
        self.assertIsNone(match_codec([(18, "G729", 8000)], [PCMU]))


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(data)


class RtpTest(unittest.TestCase):
    def pair(self, codec=PCMU):
        a_sock = FakeSock()
        a = RtpSession(a_sock, codec, ("10.0.0.2", 5000))
        b = RtpSession(FakeSock(), codec, ("10.0.0.1", 4000))
        return a, a_sock, b

    def test_gap_concealed_with_repeat_and_late_dropped(self):
        a, wire, b = self.pair()
        blocks = [np.full(160, 1000 * (i + 1), np.int16) for i in range(4)]
        for blk in blocks:
            a.send(blk)
        p = wire.sent
        got = b.receive(p[0], ("10.0.0.1", 4000)) + b.receive(p[2], ("10.0.0.1", 4000))
        self.assertEqual([len(g) for g in got], [160, 160, 160])            # packet 0, concealment, packet 2
        self.assertEqual(int(got[1][0]), int(got[0][0]))                    # repeat of the last frame
        self.assertEqual(b.receive(p[1], ("10.0.0.1", 4000)), [])            # late
        self.assertEqual(b.receive(p[3], ("10.0.0.9", 4000)), [])            # foreign address
        self.assertEqual(b.stats["late_or_duplicate"], 1)

    def test_l16_network_order(self):
        a, wire, b = self.pair(L16_16K)
        x = np.arange(320, dtype=np.int16) * 50
        a.send(x)
        self.assertEqual(wire.sent[0][12:14], (0).to_bytes(2, "big"))
        np.testing.assert_array_equal(b.receive(wire.sent[0], ("10.0.0.1", 4000))[0], x)


class SipLoopbackTest(unittest.TestCase):
    """Two legs on localhost: one listens, the other dials it directly (point-to-point link)."""

    def test_p2p_call_carries_audio_and_bye(self):
        b = SipLeg(listen=True, sip_port=0, local_ip="127.0.0.1")
        a = SipLeg(dial=f"station@127.0.0.1:{b.sip_port}", local_ip="127.0.0.1", redial_after=60)
        got = []
        deadline = time.monotonic() + 5
        tone = (np.sin(np.arange(160) * 0.9) * 8000).astype(np.int16)
        while time.monotonic() < deadline and len(got) < 5:
            r, _, _ = select.select(a.fds() + b.fds(), [], [], 0.02)
            a.service(r)
            for blk in b.service(r):
                got.append(blk)
            if a.state == "up":
                a.send(tone)
        self.assertEqual((a.state, b.state), ("up", "up"))
        self.assertGreaterEqual(len(got), 5)
        self.assertLess(np.max(np.abs(got[-1].astype(int) - tone)), 300)
        a.hangup()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and b.state == "up":
            r, _, _ = select.select(b.fds(), [], [], 0.05)
            b.service(r)
        self.assertEqual(b.state, "idle")
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
