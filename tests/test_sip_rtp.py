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

    def test_peer_ptime_differs_from_ours(self):
        # We send 10 ms, the peer (Asterisk) sends 20 ms: no gap must be invented between its packets.
        a_sock = FakeSock()
        a = RtpSession(a_sock, L16_16K, ("10.0.0.2", 5000), frame_ms=20)
        b = RtpSession(FakeSock(), L16_16K, ("10.0.0.1", 4000), frame_ms=10)
        for _ in range(3):
            a.send(np.ones(320, np.int16))
        got = [blk for p in a_sock.sent for blk in b.receive(p, ("10.0.0.1", 4000))]
        self.assertEqual([len(g) for g in got], [320, 320, 320])
        self.assertEqual(b.stats["concealed_samples"], 0)

    def test_l16_network_order(self):
        a, wire, b = self.pair(L16_16K)
        x = np.arange(320, dtype=np.int16) * 50
        a.send(x)
        self.assertEqual(wire.sent[0][12:14], (0).to_bytes(2, "big"))
        np.testing.assert_array_equal(b.receive(wire.sent[0], ("10.0.0.1", 4000))[0], x)

    def test_sender_drop_advances_rtp_sequence_and_timestamp(self):
        a, wire, b = self.pair()
        block = np.full(160, 1000, np.int16)
        a.send(block)
        a.send(block, drop=True)
        a.send(block)

        self.assertEqual(len(wire.sent), 2)
        seq0 = int.from_bytes(wire.sent[0][2:4], "big")
        seq2 = int.from_bytes(wire.sent[1][2:4], "big")
        ts0 = int.from_bytes(wire.sent[0][4:8], "big")
        ts2 = int.from_bytes(wire.sent[1][4:8], "big")
        self.assertEqual((seq2 - seq0) & 0xFFFF, 2)
        self.assertEqual((ts2 - ts0) & 0xFFFFFFFF, 320)
        self.assertEqual(a.stats["simulated_dropped"], 1)
        received = b.receive(wire.sent[0], ("10.0.0.1", 4000))
        received += b.receive(wire.sent[1], ("10.0.0.1", 4000))
        self.assertEqual(len(received), 3)  # first, one RTP-gap concealment block, then last
        self.assertEqual(b.stats["concealed_samples"], 160)


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

    def test_lab_tx_silence_preserves_rtp_send_with_zero_payload_audio(self):
        class CaptureRtp:
            def __init__(self):
                self.blocks = []
                self.drops = []

            def send(self, pcm, *, drop=False):
                self.blocks.append(np.array(pcm, copy=True))
                self.drops.append(drop)

        leg = SipLeg.__new__(SipLeg)
        leg.rtp = CaptureRtp()
        leg.state = "up"
        leg.rate = 8000
        leg.sim_mask_tone_hz = 0.0
        leg.sim_tx_gain = 1.0
        leg.sim_tx_silence_every = 2
        leg.sim_tx_drop_every = 0
        leg._tx_active_blocks = 0
        leg.tx_silenced_blocks = 0
        tone = np.full(160, 1200, np.int16)

        leg.send(tone)
        leg.send(np.zeros(160, np.int16))  # idle silence does not affect the active-block cadence
        leg.send(tone)

        self.assertEqual(len(leg.rtp.blocks), 3)
        np.testing.assert_array_equal(leg.rtp.blocks[0], tone)
        np.testing.assert_array_equal(leg.rtp.blocks[1], np.zeros(160, np.int16))
        np.testing.assert_array_equal(leg.rtp.blocks[2], np.zeros(160, np.int16))
        self.assertEqual(leg.tx_silenced_blocks, 1)

    def test_lab_tx_drop_omits_selected_active_rtp_packets(self):
        class CaptureRtp:
            def __init__(self):
                self.blocks = []
                self.drops = []

            def send(self, pcm, *, drop=False):
                self.blocks.append(np.array(pcm, copy=True))
                self.drops.append(drop)

        leg = SipLeg.__new__(SipLeg)
        leg.rtp = CaptureRtp()
        leg.state = "up"
        leg.rate = 8000
        leg.sim_mask_tone_hz = 0.0
        leg.sim_tx_gain = 1.0
        leg.sim_tx_silence_every = 0
        leg.sim_tx_drop_every = 2
        leg._tx_active_blocks = 0
        leg.tx_silenced_blocks = 0
        leg.tx_dropped_blocks = 0
        tone = np.full(160, 1200, np.int16)

        for _ in range(3):
            leg.send(tone)

        self.assertEqual(leg.rtp.drops, [False, True, False])
        self.assertEqual(leg.tx_dropped_blocks, 1)

    def test_lab_tx_gain_scales_audio_before_rtp(self):
        class CaptureRtp:
            def __init__(self):
                self.blocks = []

            def send(self, pcm, *, drop=False):
                self.blocks.append(np.array(pcm, copy=True))

        leg = SipLeg.__new__(SipLeg)
        leg.rtp = CaptureRtp()
        leg.state = "up"
        leg.rate = 8000
        leg.sim_mask_tone_hz = 0.0
        leg.sim_tx_gain = 0.1
        leg.sim_tx_silence_every = 0
        leg.sim_tx_drop_every = 0
        leg._tx_active_blocks = 0
        leg.tx_silenced_blocks = 0
        leg.tx_dropped_blocks = 0
        leg.send(np.full(160, 10000, np.int16))

        np.testing.assert_array_equal(leg.rtp.blocks[0], np.full(160, 1000, np.int16))

    def test_lab_mask_tone_replaces_only_silent_blocks(self):
        class CaptureRtp:
            def __init__(self):
                self.blocks = []

            def send(self, pcm, *, drop=False):
                self.blocks.append(np.array(pcm, copy=True))

        leg = SipLeg.__new__(SipLeg)
        leg.rtp = CaptureRtp()
        leg.state = "up"
        leg.rate = 8000
        leg.sim_mask_tone_hz = 3000.0
        leg.sim_mask_amplitude = 1000.0
        leg._mask_phase = 0
        leg.tx_masker_blocks = 0
        leg.sim_tx_gain = 1.0
        leg.sim_tx_silence_every = 0
        leg.sim_tx_drop_every = 0
        leg._tx_active_blocks = 0
        leg.tx_silenced_blocks = 0
        leg.tx_dropped_blocks = 0

        leg.send(np.zeros(160, np.int16))
        data = np.full(160, 1234, np.int16)
        leg.send(data)

        self.assertGreater(np.sqrt(np.mean(leg.rtp.blocks[0].astype(float) ** 2)), 500)
        np.testing.assert_array_equal(leg.rtp.blocks[1], data)
        self.assertEqual(leg.tx_masker_blocks, 1)

    def test_lab_mask_wav_loops_and_scales_only_silent_blocks(self):
        class CaptureRtp:
            def __init__(self):
                self.blocks = []

            def send(self, pcm, *, drop=False):
                self.blocks.append(np.array(pcm, copy=True))

        leg = SipLeg.__new__(SipLeg)
        leg.rtp = CaptureRtp()
        leg.state = "up"
        leg.rate = 8000
        leg.sim_mask_tone_hz = 0.0
        leg._mask_audio = np.array([1000, 2000, 3000, 4000], np.int16)
        leg._mask_audio_offset = 0
        leg.sim_mask_gain = 0.5
        leg.tx_masker_blocks = 0
        leg.sim_tx_gain = 1.0
        leg.sim_tx_silence_every = 0
        leg.sim_tx_drop_every = 0
        leg._tx_active_blocks = 0
        leg.tx_silenced_blocks = 0
        leg.tx_dropped_blocks = 0

        leg.send(np.zeros(3, np.int16))
        speech = np.full(3, 1234, np.int16)
        leg.send(speech)
        leg.send(np.zeros(3, np.int16))

        np.testing.assert_array_equal(leg.rtp.blocks[0], [500, 1000, 1500])
        np.testing.assert_array_equal(leg.rtp.blocks[1], speech)
        np.testing.assert_array_equal(leg.rtp.blocks[2], [2000, 500, 1000])
        self.assertEqual(leg.tx_masker_blocks, 2)


if __name__ == "__main__":
    unittest.main()
