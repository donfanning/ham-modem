"""PACTOR-I through the lab ConfBridge room (SIP, 8 kHz G.711): two ham-modem terminals join room 7300,
one calls the other (ARQ), text goes both ways, FEC broadcast is heard by the other.

    PYTHONPATH=. .venv/bin/python lab/pactor_confbridge.py
"""
import socket
import subprocess
import sys
import time

PBX, ROOM = "172.31.73.10", 7300


def connect(port, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.settimeout(timeout)
            return s
        except (ConnectionRefusedError, OSError):
            time.sleep(0.3)
    raise RuntimeError(f"cannot connect to 127.0.0.1:{port}")


def read(s, timeout=12):
    s.settimeout(timeout)
    parts = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = s.recv(4096)
            if data:
                parts.append(data)
                if b"\n" in data:
                    break
        except socket.timeout:
            break
    return b"".join(parts)


def drain(s, timeout=1.0):
    """Read everything available within timeout."""
    s.settimeout(timeout)
    parts = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = s.recv(4096)
            if data:
                parts.append(data)
        except socket.timeout:
            break
    return b"".join(parts)


def start(call, tcp):
    return subprocess.Popen(
        [sys.executable, "-m", "hammodem.main", "--modem", "pactor", "--mycall", call,
         "--sip-dial", f"{ROOM}@{PBX}", "--local-ip", "172.31.73.1", "--text-tcp", f"127.0.0.1:{tcp}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    procs = [start("W1AW", 9601), start("G3PLX", 9602)]
    ok = True
    try:
        time.sleep(2)
        a, b = connect(9601), connect(9602)
        drain(a)
        drain(b)
        time.sleep(4)  # let both SIP legs fully establish in the ConfBridge

        # FEC test — send several packets so the ConfBridge path has multiple chances
        print("--- FEC test ---")
        a.sendall(b"/fec\r\n")
        time.sleep(1)
        for _ in range(3):
            a.sendall(b"CQ CQ DE W1AW K\r\n")
            time.sleep(3)
        fec = drain(b, 6)
        print(f"  B heard: {fec!r}")
        if b"CQ" in fec:
            print("  FEC: PASS")
        else:
            print("  FEC: FAIL (did not hear CQ)")
            ok = False

        # ARQ test
        print("\n--- ARQ test ---")
        a.sendall(b"/arq\r\n")
        time.sleep(0.5)
        a.sendall(b"/connect G3PLX\r\n")
        time.sleep(15)
        arq_a = drain(a, 4)
        print(f"  A connect status: {arq_a!r}")

        a.sendall(b"HELLO G3PLX THIS IS W1AW 73\r\n")
        time.sleep(15)
        arq_b = drain(b, 4)
        print(f"  B received: {arq_b!r}")
        if b"HELLO" in arq_b:
            print("  ARQ A->B: PASS")
        else:
            print("  ARQ A->B: FAIL")
            ok = False

        b.sendall(b"HELLO W1AW COPY 5/9\r\n")
        time.sleep(20)
        arq_a2 = drain(a, 6)
        print(f"  A received: {arq_a2!r}")
        if b"COPY" in arq_a2 or b"HELLO" in arq_a2:
            print("  ARQ B->A: PASS")
        else:
            print("  ARQ B->A: FAIL")
            ok = False

        a.sendall(b"/disconnect\r\n")
        time.sleep(8)
        disc = drain(a, 4)
        print(f"  Disconnect: {disc!r}")

    finally:
        for p in procs:
            p.terminate()
            p.wait(10)

    print(f"\n{'ALL TESTS PASSED' if ok else 'SOME TESTS FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
