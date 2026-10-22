"""Frame success of the codec2 datac modes versus SNR (3 kHz, ITU convention) on the simulated HF channels.

    PYTHONPATH=. .venv/bin/python lab/freedata/sweep.py [PRESET ...]      (default: awgn moderate poor)

Each point is BURSTS single-frame bursts through hammodem.hfchannel (µ-law not applied); the table gives the
fraction of frames received with a correct CRC.
"""
import sys

import numpy as np

from hammodem.freedata.codec2 import MODES, Datac
from hammodem.hfchannel import HfChannel

BURSTS = 10
SNRS = list(range(-8, 21, 2))


def trial(mode, preset, snr, seed):
    tx = Datac(mode, 1)
    payload = bytes((seed * 13 + k) % 256 for k in range(tx.payload_bytes))
    x = np.concatenate((np.zeros(2000), tx.modulate([payload]).astype(float), np.zeros(16000)))
    ch = HfChannel(8000, preset, snr_db=snr, seed=seed, signal_dbfs=-12)
    y = np.concatenate([ch.process(x[i:i + 160]) for i in range(0, len(x), 160)])
    got = Datac(mode, 1).feed(np.clip(np.round(y), -32768, 32767).astype(np.int16))
    return any(g.crc_ok and g.payload == payload for g in got)


def main():
    presets = sys.argv[1:] or ["awgn", "moderate", "poor"]
    modes = []
    for m in MODES:
        try:
            Datac(m).close()
            modes.append(m)
        except OSError:
            pass
    for preset in presets:
        print(f"\n{preset}: fraction of {BURSTS} frames decoded")
        print("SNR dB  " + " ".join(f"{m:>8s}" for m in modes))
        for snr in SNRS:
            row = [sum(trial(m, preset, snr, s) for s in range(BURSTS)) / BURSTS for m in modes]
            print(f"{snr:6d}  " + " ".join(f"{r:8.1f}" for r in row), flush=True)


if __name__ == "__main__":
    main()
