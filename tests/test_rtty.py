import shutil
import subprocess
import tempfile
import unittest
import wave

import numpy as np

from hammodem.audio import g711
from hammodem.keyboard.rtty import RttyDemodulator, RttyModulator, encode_text
from hammodem.keyboard.tnc import KeyboardTnc

MSG = "RYRYRY CQ CQ DE W1AW 599 73 (0123456789) ?\r\n"


def run_demod(x, **kw):
    d, out = RttyDemodulator(**kw), ""
    for i in range(0, len(x), 160):
        out += d.feed(x[i:i + 160])
    return out, d


def transmit(msg=MSG, **kw):
    m = RttyModulator(**kw)
    return np.concatenate((m.idle_samples(0.4), m.text(msg), m.idle_samples(0.4))).astype(float)


class RttyTest(unittest.TestCase):
    def test_text_conversion_round_trip(self):
        from hammodem.keyboard.rtty import Decoder
        codes, _ = encode_text("MAYDAY 123, DE N0CALL (OK)?\r\n")
        d = Decoder()
        self.assertEqual("".join(d.char(c) for c in codes), "MAYDAY 123, DE N0CALL (OK)?\r\n")

    def test_standard_rates_and_shifts(self):
        for baud, shift in ((45.45, 170), (50, 170), (75, 425), (100, 850)):
            kw = dict(baud=baud, shift=shift)
            text, d = run_demod(np.concatenate((np.zeros(777), transmit(**kw), np.zeros(3000))), **kw)
            self.assertEqual(text, MSG.replace("\r", "").replace("\n", "\n").replace("\n", "\r\n").replace("\r\n", "\n") if False else MSG, kw)

    def test_clock_error_noise_and_ulaw(self):
        x = transmit()
        n = len(x)
        for ppm in (-400, 400):
            y = np.interp(np.arange(int(n * (1 - ppm * 1e-6))) * (1 + ppm * 1e-6), np.arange(n), x)
            self.assertEqual(run_demod(np.concatenate((np.zeros(500), y, np.zeros(2000))))[0], MSG, ppm)
        noisy = x + np.random.default_rng(1).normal(0, 2500, n)
        noisy = g711.decode(g711.encode(np.round(noisy), "pcmu"), "pcmu").astype(float)
        text, _ = run_demod(noisy)
        wrong = sum(a != b for a, b in zip(text, MSG)) + abs(len(text) - len(MSG))
        self.assertLessEqual(wrong, 2, text)

    def test_reverse(self):
        self.assertEqual(run_demod(transmit(reverse=True), reverse=True)[0], MSG)
        self.assertNotEqual(run_demod(transmit(reverse=True))[0], MSG)

    @unittest.skipUnless(shutil.which("minimodem"), "minimodem not installed")
    def test_interop_with_minimodem(self):
        with tempfile.TemporaryDirectory() as tmp:
            # ours -> minimodem
            x = np.round(transmit(center=1500)).astype("<i2")
            path = f"{tmp}/ours.wav"
            with wave.open(path, "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000), w.writeframes(x.tobytes())
            out = subprocess.run(["minimodem", "--rx", "rtty", "-f", path], capture_output=True, text=True).stdout
            self.assertIn("RYRYRY CQ CQ DE W1AW 599 73", out)
            # minimodem -> ours
            path2 = f"{tmp}/mm.wav"
            subprocess.run(["minimodem", "--tx", "rtty", "-f", path2, "-R", "8000"], input="CQ TEST DE N0CALL 123\n",
                           text=True, capture_output=True)
            with wave.open(path2) as w:
                y = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
            text, _ = run_demod(y, center=1500)
            self.assertIn("CQ TEST DE N0CALL 123", text)


class TerminalTest(unittest.TestCase):
    def test_two_terminals(self):
        got = {"a": b"", "b": b""}
        a = KeyboardTnc("rtty", RttyModulator(), RttyDemodulator(),
                        write=lambda d: got.__setitem__("a", got["a"] + d))
        b = KeyboardTnc("rtty", RttyModulator(), RttyDemodulator(),
                        write=lambda d: got.__setitem__("b", got["b"] + d))
        a.feed_host(b"HELLO B\r\n")
        for _ in range(50 * 8):
            b.audio_in(a.audio_out(160))
            a.audio_in(b.audio_out(160))
        self.assertEqual(got["b"], b"HELLO B\r\n")


if __name__ == "__main__":
    unittest.main()
