"""Two ham-modem AMTOR terminals joined by UDP audio on loopback; drives them through their text TCP ports.

    PYTHONPATH=. .venv/bin/python lab/amtor_loopback.py
"""
import select
import socket
import subprocess
import sys
import time


def start(call, lport, peer, tcp):
    return subprocess.Popen([sys.executable, "-m", "hammodem.main", "--modem", "amtor", "--mycall", call,
                             "--udp-audio", f"{lport}:127.0.0.1:{peer}", "--udp-bind", "127.0.0.1",
                             "--text-tcp", f"127.0.0.1:{tcp}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect(port):
    for _ in range(50):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.setblocking(False)
            return s
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("no TNC")


def read(s, seconds):
    out, end = b"", time.monotonic() + seconds
    while time.monotonic() < end:
        if select.select([s], [], [], 0.1)[0]:
            out += s.recv(4096)
    return out.decode(errors="replace")


def main():
    procs = [start("W1AW", 7501, 7502, 9501), start("G3PLX", 7502, 7501, 9502)]
    try:
        a, b = connect(9501), connect(9502)
        a.sendall(b"/fec\r\nCQ CQ DE W1AW K\r\n")
        print("FEC  B heard:", repr(read(b, 14)))
        a.sendall(b"/arq\r\n/connect G3PLX\r\n")
        print("ARQ  A:", repr(read(a, 8)))
        a.sendall(b"HELLO G3PLX THIS IS W1AW 73 +?\r\n")
        print("ARQ  B:", repr(read(b, 12)))
        b.sendall(b"HELLO W1AW, COPY 5/9\r\n/disconnect\r\n")
        print("ARQ  A:", repr(read(a, 14)))
        print("ARQ  B:", repr(read(b, 6)))
    finally:
        for p in procs:
            p.terminate()
            p.wait(10)


if __name__ == "__main__":
    main()
