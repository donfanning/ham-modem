"""Live ARQ interop: ham-modem's ArqStation against a running ardopcf (lab/ardop/run_peer.sh) on the lab network.

The relay in the ardopcf container sends one 20 ms block of ardopcf's audio every 20 ms; each block goes through
our station and our block goes back, so both run on the relay's real-time clock. ardopcf is driven over its host
interface (commands on TCP 8515, data on 8516 with a 2-byte length prefix).

    PYTHONPATH=. .venv/bin/python lab/ardop/live_arq.py {they-call|we-call} [BYTES_TO_US] [BYTES_TO_THEM] [BW]
        [DEADLINE_S] [RECORDING.npz] [DRIVE]
"""
import select
import socket
import sys
import time

import numpy as np

from hammodem.ardop.arq import ArqStation

PEER, HOST = "172.31.73.40", "172.31.73.1"
THEM, US = "N0CALL", "K1ABC"


class Ardopcf:
    def __init__(self):
        self.c = socket.create_connection((PEER, 8515), timeout=5)
        self.d = socket.create_connection((PEER, 8516), timeout=5)
        self.c.setblocking(False)
        self.d.setblocking(False)
        self.cbuf, self.dbuf, self.events, self.received = b"", b"", [], bytearray()

    def cmd(self, s):
        self.c.sendall(s.encode() + b"\r")

    def send(self, data):
        for i in range(0, len(data), 1000):
            chunk = data[i:i + 1000]
            self.d.sendall(len(chunk).to_bytes(2, "big") + chunk)

    def poll(self, t):
        for sock in (self.c, self.d):
            try:
                chunk = sock.recv(65536)
            except BlockingIOError:
                continue
            if sock is self.c:
                self.cbuf += chunk
                while b"\r" in self.cbuf:
                    line, self.cbuf = self.cbuf.split(b"\r", 1)
                    self.events.append((round(t, 1), line.decode(errors="replace")))
                    if not line.startswith((b"PTT", b"BUFFER", b"BUSY", b"RDY")):
                        print("   ardopcf", round(t, 1), line.decode(errors="replace"), flush=True)
            else:
                self.dbuf += chunk
                while len(self.dbuf) >= 2 and len(self.dbuf) >= 2 + int.from_bytes(self.dbuf[:2], "big"):
                    n = int.from_bytes(self.dbuf[:2], "big")
                    frame, self.dbuf = self.dbuf[2:2 + n], self.dbuf[2 + n:]
                    if frame[:3] == b"ARQ":
                        self.received += frame[3:]


def main():
    mode = sys.argv[1]
    n_us = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    n_them = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    bw = sys.argv[4] if len(sys.argv) > 4 else "500MAX"
    rng = np.random.default_rng(1)
    to_us = bytes(rng.integers(32, 127, n_us, dtype=np.uint8))       # printable: ardopcf logs data as text
    to_them = bytes(rng.integers(32, 127, n_them, dtype=np.uint8))
    au = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    au.bind((HOST, 7401))
    au.setblocking(False)
    peer = Ardopcf()
    t0 = time.monotonic()
    log = []
    drive = float(sys.argv[7]) if len(sys.argv) > 7 else 1.0
    st = ArqStation(US, fs=12000, bandwidth=bw, seed=1, drive=drive, on_event=lambda e: (log.append((round(st.now / 1000, 1), e)),
                    e.startswith(("PTT", "BUFFER")) or print("   us", round(time.monotonic() - t0, 1), e, flush=True)))
    for c in ("INITIALIZE", f"MYCALL {THEM}", "PROTOCOLMODE ARQ", f"ARQBW {bw}", "LISTEN TRUE", "ARQTIMEOUT 60"):
        peer.cmd(c)
    time.sleep(1)
    started = False
    phase = "start"
    rx_rec, tx_rec = [], []
    deadline = int(sys.argv[5]) if len(sys.argv) > 5 else 300
    while time.monotonic() - t0 < deadline:
        r, _, _ = select.select([au, peer.c, peer.d], [], [], 0.05)
        t = time.monotonic() - t0
        peer.poll(t)
        if au in r:
            while True:
                try:
                    blk, _ = au.recvfrom(4096)
                except BlockingIOError:
                    break
                x = np.frombuffer(blk, "<i2")
                rx_rec.append(x)
                out = st.process(x.astype(float))
                tx_rec.append(out)
                au.sendto(out.astype("<i2").tobytes(), (PEER, 7400))
        if not started and t > 3:
            started = True
            if mode == "they-call":
                peer.send(to_us)
                peer.cmd(f"ARQCALL {US} 5")
            else:
                st.send(to_them)
                st.connect(THEM)
            phase = "first"
        if phase == "first":
            if mode == "they-call" and len(st.received) >= n_us:
                st.send(to_them)
                phase = "second"
            if mode == "we-call" and len(peer.received) >= n_them:
                peer.send(to_us)
                phase = "second"
        if phase == "second":
            if len(st.received) >= n_us and len(peer.received) >= n_them:
                phase = "disc"
                t_done = t
                (peer.cmd("DISCONNECT") if mode == "they-call" else st.disconnect())
        if phase == "disc" and st.state == "DISC" and any("DISCONNECTED" in e for _, e in peer.events) \
                and t > t_done + 6:
            break
    print("--- ardopcf events")
    for e in peer.events:
        if not e[1].startswith(("PTT", "BUFFER", "BUSY", "RDY")):
            print("  ", e)
    if len(sys.argv) > 6:
        np.savez(sys.argv[6], rx=np.concatenate(rx_rec), tx=np.concatenate(tx_rec))
    print("--- receiver: keyed", st.keyed, "need", st.phy._need, "buf", len(st.phy._buf), "scan", st.phy._scan,
          "offset", st.phy._offset, "txq", len(st._txq), "now", round(st.now))
    print("--- frames we heard (ms, frame, ok, session, accepted, state, sub)")
    for tr in st.trace:
        print("  ", tr)
    print("--- our events")
    for e in log:
        if not e[1].startswith(("PTT", "BUFFER")):
            print("  ", e)
    ok_us, ok_them = bytes(st.received) == to_us, bytes(peer.received) == to_them
    print(f"RESULT {mode} {bw}: to us {len(st.received)}/{n_us} intact={ok_us}; to ardopcf "
          f"{len(peer.received)}/{n_them} intact={ok_them}; elapsed {time.monotonic() - t0:.0f}s; "
          f"our state {st.state}; stats {st.stats}; modes {st.modes_used}; "
          f"ardopcf input warnings {sum('INPUTPEAKS' in e for _, e in peer.events)}")


if __name__ == "__main__":
    main()
