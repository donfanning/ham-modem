"""AMTOR through the lab ConfBridge room (SIP, 8 kHz G.711): two ham-modem terminals join room 7300, one calls the
other (ARQ), text goes both ways, FEC broadcast is heard by the other.

    PYTHONPATH=. .venv/bin/python lab/amtor_confbridge.py [REPLY_MS]
"""
import sys

from amtor_loopback import connect, read
import subprocess

PBX, ROOM = "172.31.73.10", 7300


def start(call, tcp, reply):
    return subprocess.Popen([sys.executable, "-m", "hammodem.main", "--modem", "amtor", "--mycall", call,
                             "--sip-dial", f"{ROOM}@{PBX}", "--local-ip", "172.31.73.1", "--text-tcp",
                             f"127.0.0.1:{tcp}", "--amtor-reply", str(reply)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    reply = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    procs = [start("W1AW", 9601, reply), start("G3PLX", 9602, reply)]
    try:
        a, b = connect(9601), connect(9602)
        read(a, 6)
        a.sendall(b"/fec\r\nCQ CQ DE W1AW K\r\n")
        print("FEC  B heard:", repr(read(b, 14)))
        a.sendall(b"/arq\r\n/connect G3PLX\r\n")
        print("ARQ  A:", repr(read(a, 10)))
        a.sendall(b"HELLO G3PLX THIS IS W1AW 73 +?\r\n")
        print("ARQ  B:", repr(read(b, 14)))
        b.sendall(b"HELLO W1AW, COPY 5/9\r\n/disconnect\r\n")
        print("ARQ  A:", repr(read(a, 16)))
        print("ARQ  B:", repr(read(b, 6)))
    finally:
        for p in procs:
            p.terminate()
            p.wait(10)


if __name__ == "__main__":
    main()
