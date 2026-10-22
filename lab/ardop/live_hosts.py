"""Live: ham-modem's ARDOP TNC (``ham-modem --modem ardop``) against ardopcf, each driven only through its
ardopcf-style host interface by the same client code -- what a Winlink client sees.

Start ardopcf (lab/ardop/run_peer.sh, container ham-lab-ardopcf at 172.31.73.40) first, then:

    PYTHONPATH=. .venv/bin/python lab/ardop/live_hosts.py [BYTES_A_TO_B] [BYTES_B_TO_A] [ARQBW] [CALLER] [--external-tnc]

A is ours (K1ABC, host interface 127.0.0.1:8515, UDP audio on 172.31.73.1:7401), B is ardopcf (N0CALL). CALLER is
"ours" (default) or "ardopcf".
"""
import select
import socket
import subprocess
import sys
import time

import numpy as np


class Host:
    def __init__(self, name, addr):
        self.name = name
        for _ in range(50):
            try:
                self.c = socket.create_connection((addr, 8515), timeout=5)
                self.d = socket.create_connection((addr, 8516), timeout=5)
                break
            except OSError:
                time.sleep(0.2)
        self.c.setblocking(False)
        self.d.setblocking(False)
        self.lines, self.rx, self._cb, self._db = [], bytearray(), b"", b""

    def cmd(self, text):
        self.c.sendall(text.encode() + b"\r")

    def send(self, data):
        for i in range(0, len(data), 1000):
            chunk = data[i:i + 1000]
            self.d.sendall(len(chunk).to_bytes(2, "big") + chunk)

    def poll(self, t0):
        for s in (self.c, self.d):
            try:
                chunk = s.recv(65536)
            except BlockingIOError:
                continue
            if s is self.c:
                self._cb += chunk
                while b"\r" in self._cb:
                    line, self._cb = self._cb.split(b"\r", 1)
                    text = line.decode(errors="replace")
                    self.lines.append(text)
                    if not text.startswith(("PTT", "BUFFER", "BUSY", "RDY", "INPUTPEAKS")):
                        print(f"  {time.monotonic() - t0:6.1f} {self.name:8s} {text}", flush=True)
            else:
                self._db += chunk
                while len(self._db) >= 2 and len(self._db) >= 2 + int.from_bytes(self._db[:2], "big"):
                    n = int.from_bytes(self._db[:2], "big")
                    frame, self._db = self._db[2:2 + n], self._db[2 + n:]
                    if frame[:3] == b"ARQ":
                        self.rx += frame[3:]


def main():
    n_ab = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    n_ba = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    bw = sys.argv[3] if len(sys.argv) > 3 else "500MAX"
    caller = sys.argv[4] if len(sys.argv) > 4 else "ours"
    rng = np.random.default_rng(2)
    ab = bytes(rng.integers(32, 127, n_ab, dtype=np.uint8))
    ba = bytes(rng.integers(32, 127, n_ba, dtype=np.uint8))
    external = "--external-tnc" in sys.argv                           # the TNC is already running (VoIP legs)
    tnc = None if external else subprocess.Popen(
        [sys.executable, "-m", "hammodem.main", "--modem", "ardop", "--rate", "12000",
         "--udp-audio", "7401:172.31.73.40:7400", "--udp-bind", "172.31.73.1",
         "--ardop-host", "127.0.0.1:8515", "--mycall", "K1ABC", "--drive", "50"])
    t0 = time.monotonic()
    try:
        a, b = Host("ours", "127.0.0.1"), Host("ardopcf", "172.31.73.40")
        for h, call in ((a, "K1ABC"), (b, "N0CALL")):
            for text in ("INITIALIZE", f"MYCALL {call}", "PROTOCOLMODE ARQ", f"ARQBW {bw}", "LISTEN TRUE",
                         "ARQTIMEOUT 60"):
                h.cmd(text)
        time.sleep(1)
        first, second = (a, b) if caller == "ours" else (b, a)
        first.send(ab if first is a else ba)
        first.cmd(f"ARQCALL {'N0CALL' if first is a else 'K1ABC'} 5")
        replied = done = False
        t_done = None
        while time.monotonic() - t0 < 600:
            select.select([a.c, a.d, b.c, b.d], [], [], 0.1)
            a.poll(t0)
            b.poll(t0)
            got_first = len(second.rx) >= (n_ab if first is a else n_ba)
            if got_first and not replied:
                second.send(ba if second is b else ab)
                replied = True
            if not done and len(b.rx) >= n_ab and len(a.rx) >= n_ba:
                done, t_done = True, time.monotonic()
                first.cmd("DISCONNECT")
            if done and "DISCONNECTED" in a.lines and "DISCONNECTED" in b.lines and time.monotonic() > t_done + 5:
                break
        print(f"RESULT caller={caller} {bw}: ours->ardopcf {len(b.rx)}/{n_ab} intact={bytes(b.rx) == ab}; "
              f"ardopcf->ours {len(a.rx)}/{n_ba} intact={bytes(a.rx) == ba}; "
              f"{(t_done or time.monotonic()) - t0:.0f}s to complete")
    finally:
        if tnc:
            tnc.terminate()
            tnc.wait(10)


if __name__ == "__main__":
    main()
