# Offline: ARQ transfer (1500 B, 500 Hz, 8 kHz leg) with random 20 ms packets lost as silence, both directions.
# Run from the repo root: PYTHONPATH=. .venv/bin/python lab/ardop/rtp_loss.py   (~20 s, uses several cores)
import sys, time
import numpy as np
from hammodem.ardop.arq import ArqStation
FS=8000; BLK=160
def sess(p, seed, bw="500MAX", n=1500):
    rng=np.random.default_rng(seed)
    a=ArqStation("N0CALL",fs=FS,bandwidth=bw,seed=1); b=ArqStation("K1ABC",fs=FS,bandwidth=bw,seed=2)
    data=bytes(rng.integers(0,256,n,dtype=np.uint8)); a.send(data); a.connect("K1ABC")
    ta=tb=np.zeros(BLK)
    for t in range(60*50*6):
        ra=np.zeros(BLK) if rng.random()<p else tb
        rb=np.zeros(BLK) if rng.random()<p else ta
        ta,tb=a.process(ra).astype(float),b.process(rb).astype(float)
        if bytes(b.received)==data: return t/50, a.stats["repeats"]
    return None, a.stats["repeats"]
for p in (0,0.01,0.03,0.05):
    r=[sess(p,s) for s in range(3)]
    print(p, r, flush=True)
