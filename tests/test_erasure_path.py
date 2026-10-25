"""Verify sample-span erasures reach the FEC receivers in every supported packet waveform."""

import unittest

import numpy as np

from hammodem.ax25 import Frame
from hammodem.modems import make


class _ErasureTap:
    def __init__(self):
        self.masks = []

    def feed_bits(self, bits, erased=None):
        self.masks.append(None if erased is None else np.asarray(erased, bool).copy())
        return []


def _run_erasure(modem_name, fs, fec, receiver_attr):
    mod, dem = make(modem_name, fs)
    setattr(mod, fec, True if fec == "il2p" else 32)
    frame = Frame.ui("N0CALL", "APRS", "loss mask propagation").encode()
    audio = mod.modulate([frame], txdelay=0.1, txtail=0.02)
    taps = []
    if modem_name.startswith("afsk"):
        for slicer in dem.slicers:
            tap = _ErasureTap()
            setattr(slicer, receiver_attr, tap)
            taps.append(tap)
    else:
        tap = _ErasureTap()
        setattr(dem, receiver_attr, tap)
        taps.append(tap)

    block = 160
    start = (len(audio) * 2 // (5 * block)) * block
    end = start + max(block, len(audio) // 20 // block * block)
    for pos in range(0, len(audio), block):
        chunk = audio[pos:pos + block]
        dem.feed(chunk, erased=start <= pos < end)
    return [mask for tap in taps for mask in tap.masks]


class ErasurePropagationTest(unittest.TestCase):
    def assert_has_erased_bits(self, masks):
        self.assertTrue(any(mask is not None and mask.any() for mask in masks))

    def test_afsk_and_g3ruh_pass_erasure_masks_into_il2p(self):
        for modem, fs in (("afsk1200", 8000), ("g3ruh9600", 16000)):
            with self.subTest(modem=modem):
                masks = _run_erasure(modem, fs, "il2p", "il2p")
                self.assert_has_erased_bits(masks)

    def test_psk_passes_erasure_masks_into_il2pc(self):
        masks = _run_erasure("psk2400", 8000, "il2p", "il2p")
        self.assert_has_erased_bits(masks)

    def test_qam_passes_erasure_masks_into_fx25(self):
        masks = _run_erasure("qam38400", 16000, "fx25", "fx25")
        self.assert_has_erased_bits(masks)


if __name__ == "__main__":
    unittest.main()
