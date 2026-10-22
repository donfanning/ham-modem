"""Live FEC (broadcast) between ham-modem's ARDOP TNC and ardopcf, each driven only through its host interface:
ours broadcasts to ardopcf, then ardopcf broadcasts to ours.

Start ardopcf first (lab/ardop/run_peer.sh, container ham-lab-ardopcf), then:

    PYTHONPATH=. .venv/bin/python lab/ardop/live_fec.py [FECMODE] [REPEATS] [BYTES]
"""
import select
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from live_hosts import Host  # noqa: E402


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "4FSK.500.100"
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    rng = np.random.default_rng(4)
    tnc = subprocess.Popen(
        [sys.executable, "-m", "hammodem.main", "--modem", "ardop", "--rate", "12000",
         "--udp-audio", "7401:172.31.73.40:7400", "--udp-bind", "172.31.73.1",
         "--ardop-host", "127.0.0.1:8515", "--mycall", "K1ABC", "--drive", "50", "-v"])
    t0 = time.monotonic()
    try:
        a, b = Host("ours", "127.0.0.1"), Host("ardopcf", "172.31.73.40")
        for h, call in ((a, "K1ABC"), (b, "N0CALL")):
            for text in ("INITIALIZE", f"MYCALL {call}", "PROTOCOLMODE FEC", f"FECMODE {mode}",
                         f"FECREPEATS {repeats}", "FECID TRUE"):
                h.cmd(text)
        time.sleep(1)
        results = []
        for tx, rx in ((a, b), (b, a))[int(sys.argv[4]) if len(sys.argv) > 4 else 0:]:
            msg = bytes(rng.integers(32, 127, n, dtype=np.uint8))
            rx.tags.clear()
            mark = len(tx.lines)
            tx.send(msg)
            time.sleep(0.5)
            tx.cmd("FECSEND TRUE")
            start = time.monotonic()
            while time.monotonic() - start < 240:
                select.select([a.c, a.d, b.c, b.d], [], [], 0.1)
                a.poll(t0)
                b.poll(t0)
                if bytes(rx.tags.get("FEC", b"")) == msg and "IDF" in rx.tags:
                    break
            while time.monotonic() - start < 240 and not any(x.startswith("NEWSTATE DISC") for x in tx.lines[mark:]):
                select.select([a.c, a.d, b.c, b.d], [], [], 0.1)     # let the sender finish its repeats
                a.poll(t0)
                b.poll(t0)
            time.sleep(3)
            a.poll(t0)
            b.poll(t0)
            results.append(f"{tx.name}->{rx.name}: FEC {len(rx.tags.get('FEC', b''))}/{n} intact="
                           f"{bytes(rx.tags.get('FEC', b'')) == msg} ERR={len(rx.tags.get('ERR', b''))} "
                           f"IDF={bytes(rx.tags.get('IDF', b''))[:40]!r} {time.monotonic() - start:.0f}s")
        print("RESULT", mode, f"repeats={repeats}")
        for r in results:
            print("  ", r)
    finally:
        tnc.terminate()
        tnc.wait(10)


if __name__ == "__main__":
    main()
