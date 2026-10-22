"""AX.25 UI frames over the codec2 datac modes through the lab ConfBridge room: two ham-modem processes join the
room by SIP (8 kHz G.711), a script plays a KISS host on each (KISS over TCP) and sends frames both ways.

    PYTHONPATH=. .venv/bin/python lab/freedata/confbridge_datac_test.py [MODE ...]    (default: datac0 datac3 datac4)
"""
import os
import select
import socket
import subprocess
import sys
import threading
import time

from hammodem import kiss
from hammodem.ax25 import Frame

PBX = "172.31.73.10"
ROOM = 7300
SIZES = tuple(int(x) for x in os.environ.get("DATAC_SIZES", "40,300").split(","))
LOG = os.environ.get("DATAC_LOG", "")        # prefix: write each TNC's debug log to PREFIX.CALL


def capture(call, port):
    """With DATAC_LOG set, also keep each TNC's received audio (--audio-tap) in PREFIX.CALL.raw for offline analysis."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))

    def run():
        with open(f"{LOG}.{call}.raw", "wb") as f:
            while True:
                f.write(sock.recv(65536))
    threading.Thread(target=run, daemon=True).start()


def start(mode, call, port):
    if LOG:
        capture(call, port + 100)
    return subprocess.Popen(
        [sys.executable, "-m", "hammodem.main", "--modem", mode, "--mycall", call, "--kiss-tcp", f"127.0.0.1:{port}",
         "--sip-dial", f"{ROOM}@{PBX}", "--local-ip", "172.31.73.1", "--collision-detect", "--txdelay", "100", "--repeat", os.environ.get("DATAC_REPEAT", "1")] + (["-v", "--audio-tap", f"127.0.0.1:{port + 100}"] if LOG else []),
        stdout=subprocess.DEVNULL, stderr=open(f"{LOG}.{call}", "w") if LOG else subprocess.DEVNULL)


def connect(port):
    for _ in range(50):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("TNC did not start")


def receive(sock, dec, want, timeout):
    got, end = [], time.monotonic() + timeout
    while time.monotonic() < end and len(got) < want:
        r, _, _ = select.select([sock], [], [], 0.2)
        if r:
            for _, cmd, payload in dec.feed(sock.recv(65536)):
                if cmd == kiss.DATA:
                    got.append(payload)
    return got


def main():
    modes = sys.argv[1:] or ["datac0", "datac3", "datac4"]
    for mode in modes:
        procs = [start(mode, "K1ABC", 8101), start(mode, "N0CALL", 8102)]
        try:
            a, b = connect(8101), connect(8102)
            da, db = kiss.Decoder(), kiss.Decoder()
            time.sleep(6)                                          # SIP legs up
            results = []
            for name, src, dst, tx, rx, dec in (("K1ABC->N0CALL", "K1ABC", "N0CALL", a, b, db),
                                                ("N0CALL->K1ABC", "N0CALL", "K1ABC", b, a, da)):
                for size in SIZES:
                    frame = Frame.ui(src, dst, bytes((k * 7) % 256 for k in range(size))).encode()
                    t0 = time.monotonic()
                    tx.sendall(kiss.encode(frame, 0))
                    got = receive(rx, dec, 1, 120)
                    results.append(f"{name} {size:3d} B info: {'intact' if got == [frame] else 'LOST'} "
                                   f"{time.monotonic() - t0:.1f} s")
            print(mode)
            for r in results:
                print("  ", r, flush=True)
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                p.wait(10)
            time.sleep(2)


if __name__ == "__main__":
    main()
