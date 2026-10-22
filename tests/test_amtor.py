import unittest

import numpy as np

from hammodem.amtor import code, fec, fsk
from hammodem.audio import g711
from hammodem.hfchannel import HfChannel


def bystring(c):
    return "".join("B" if b else "Y" for b in code.bits(c))


def tx_audio(text):
    t, m = fec.FecTransmitter(), fsk.Modulator()
    t.send(text)
    out = []
    while (c := t.next_slot()) is not None:
        out.append(m.bits(code.bits(c)))
    return np.concatenate(out).astype(float)


def receive(y, rx=None):
    rx = rx or fec.FecReceiver()
    text = ""
    for i in range(0, len(y), 160):
        text += rx.feed(y[i:i + 160])
    return text, rx


class CodeTest(unittest.TestCase):
    def test_itu_table_values(self):
        for c, s in ((0x47, "BBBYYYB"),                  # A
                     (code.LTRS, "YBYBBYB"), (code.FIGS, "YBBYBBY"), (code.CR, "YYYBBBB"), (code.LF, "YYBBYBB"),
                     (code.SPACE, "YYBBBYB"),
                     (code.CS1, "BYBYYBB"), (code.CS2, "YBYBYBB"), (code.CS3, "BYYBBYB"),
                     (code.IDLE_B, "BBYYBBY"), (code.IDLE_A, "BBBBYYY"), (code.RQ, "YBBYYBB")):
            self.assertEqual(bystring(c), s)

    def test_thirty_five_constant_weight_codes(self):
        self.assertEqual(len(code.VALID), 35)
        self.assertTrue(all(bin(c).count("1") == 4 for c in code.VALID))

    def test_text_round_trip_with_shifts(self):
        text = "MAYDAY 123 DE N0CALL, 73? (OK)\r\n"
        codes, _ = code.encode_text(text)
        d = code.Decoder()
        self.assertEqual("".join(d.char(c) for c in codes), text)


class FecTest(unittest.TestCase):
    MSG = "CQ CQ CQ DE N0CALL N0CALL PSE K 73 +?"

    def test_clean_with_offset_and_silence(self):
        x = np.concatenate((np.zeros(4321), tx_audio(self.MSG), np.zeros(8000)))
        text, rx = receive(x)
        self.assertEqual(text, self.MSG)
        self.assertIn("phased", rx.events)

    def test_two_messages_in_a_row(self):
        x = np.concatenate((np.zeros(1000), tx_audio("FIRST ONE"), np.zeros(16000), tx_audio("SECOND 22"), np.zeros(8000)))
        text, _ = receive(x)
        self.assertEqual(text, "FIRST ONESECOND 22")

    def test_clock_error(self):
        x = tx_audio(self.MSG)
        for ppm in (-300, 300):
            n = len(x)
            y = np.interp(np.arange(int(n * (1 - ppm * 1e-6))) * (1 + ppm * 1e-6), np.arange(n), x)
            text, _ = receive(np.concatenate((np.zeros(777), y, np.zeros(8000))))
            self.assertEqual(text, self.MSG, ppm)

    def test_time_diversity_repairs_a_burst_of_noise(self):
        x = np.concatenate((np.zeros(900), tx_audio("THE QUICK BROWN FOX"), np.zeros(8000)))
        x[40000:40000 + 1100] = np.random.default_rng(1).normal(0, 8000, 1100)       # wipes out ~2 characters
        text, rx = receive(x)
        self.assertEqual(text, "THE QUICK BROWN FOX")
        self.assertGreater(rx.corrected, 0)

    def test_awgn_and_ulaw(self):
        x = np.concatenate((np.zeros(900), tx_audio(self.MSG), np.zeros(8000)))
        noise = np.random.default_rng(2).normal(0, 3000, len(x))       # about 8 dB SNR in 3 kHz
        y = g711.decode(g711.encode(np.round(x + noise), "pcmu"), "pcmu").astype(float)
        text, _ = receive(y)
        self.assertEqual(text, self.MSG)

    def test_moderate_hf_channel(self):
        x = np.concatenate((np.zeros(900), tx_audio(self.MSG + " " + self.MSG), np.zeros(8000)))
        ch = HfChannel(8000, "moderate", snr_db=14, seed=5, signal_dbfs=-12)
        y = np.concatenate([ch.process(x[i:i + 160]) for i in range(0, len(x), 160)])
        text, rx = receive(y)
        want = self.MSG + " " + self.MSG
        errors = sum(a != b for a, b in zip(text, want)) + abs(len(text) - len(want))
        self.assertLessEqual(errors, 4, text)


if __name__ == "__main__":
    unittest.main()
