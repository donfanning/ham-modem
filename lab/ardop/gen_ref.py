"""Make ardopcf transmit one of every frame family, recorded with --writetxwav: the reference set for ham-modem's
native ARDOP implementation.

Run inside the ardopcf container:  python3 gen_ref.py OUTDIR

Each frame comes from a fresh ardopcf process (ALSA "null" devices, nothing is played) driven over its host
interface, so recordings cannot be mis-attributed between sends (one shared session lost sends to host-protocol
sequencing). Every WAV (12 kHz) is then labelled by ardopcf's own decoder; manifest.json maps file -> what was
sent and what ardopcf decodes from it.
"""
import json
import os
import socket
import subprocess
import sys
import time

OUT = sys.argv[1]
MODES = ["4FSK.200.50S", "4PSK.200.100S", "4PSK.200.100", "8PSK.200.100", "16QAM.200.100", "4FSK.500.100S",
         "4FSK.500.100", "4PSK.500.100", "8PSK.500.100", "16QAM.500.100", "4PSK.1000.100", "8PSK.1000.100",
         "16QAM.1000.100", "4PSK.2000.100", "8PSK.2000.100", "16QAM.2000.100", "4FSK.2000.600S", "4FSK.2000.600"]
SETUP = ("INITIALIZE", "MYCALL N0CALL", "GRIDSQUARE FN31", "LEADER 240", "TRAILER 20")


class Tnc:
    def __init__(self, port: int, outdir: str) -> None:
        self.p = subprocess.Popen(["ardopcf", "-m", "-T", "-l", outdir, str(port), "null", "null"],
                                  stdout=open(os.path.join(outdir, "ardopcf.out"), "a"), stderr=subprocess.STDOUT)
        time.sleep(1.5)
        self.cmd = socket.create_connection(("127.0.0.1", port))
        self.dat = socket.create_connection(("127.0.0.1", port + 1))
        self.cmd.settimeout(0.2)
        self.buf = b""

    def send(self, c: str) -> None:
        self.cmd.sendall(c.encode() + b"\r")

    def wait_for(self, token: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.buf += self.cmd.recv(4096)
            except socket.timeout:
                pass
            if token.encode() in self.buf:
                self.buf = self.buf[self.buf.index(token.encode()) + len(token):]
                return True
        return False

    def close(self) -> None:
        self.p.terminate()
        self.p.wait()


def run(name: str, port: int, steps) -> dict:
    d = os.path.join(OUT, name)
    os.makedirs(d, exist_ok=True)
    t = Tnc(port, d)
    for c in SETUP:
        t.send(c)
    time.sleep(3)                                       # a send right after INITIALIZE fails ("StartFEC failed")
    info = steps(t)
    time.sleep(1)
    t.close()
    wavs = sorted(f for f in os.listdir(d) if f.endswith(".wav"))
    labels = []
    for f in wavs:
        r = subprocess.run(["ardopcf", "-m", "-d", os.path.join(d, f), str(port + 2), "null", "null"],
                           capture_output=True, text=True, timeout=60)
        labels.append([l.split("] ", 1)[-1] for l in r.stdout.splitlines()
                       if "Frame Type Decode OK" in l or "Decode PASS" in l or "Decode FAIL" in l])
    return dict(info, name=name, wavs=[os.path.join(name, f) for f in wavs], decoded=labels)


def fec(mode: str, payload: bytes):
    def steps(t: Tnc) -> dict:
        for c in ("PROTOCOLMODE FEC", "FECREPEATS 0", f"FECMODE {mode}"):
            t.send(c)
        time.sleep(0.3)
        t.dat.sendall(len(payload).to_bytes(2, "big") + payload)
        t.wait_for("BUFFER", 5)
        time.sleep(0.3)
        t.send("FECSEND TRUE")
        t.wait_for("PTT FALSE", 90)
        return {"kind": "fec", "mode": mode, "payload": payload.hex()}
    return steps


def simple(kind: str, cmds):
    def steps(t: Tnc) -> dict:
        for c in cmds:
            t.send(c)
            time.sleep(0.3)
        t.wait_for("PTT FALSE", 30)
        return {"kind": kind, "commands": list(cmds)}
    return steps


manifest = []
port = 8600
for i, mode in enumerate(MODES):
    payload = f"ham-modem ARDOP reference {mode} #{i} ".encode() * 3
    manifest.append(run(f"{i:02d}_{mode}", port, fec(mode, payload)))
    port += 3
    print(manifest[-1]["name"], manifest[-1]["decoded"], flush=True)
for kind, cmds in (("idframe", ["SENDID"]), ("ping", ["PING K1ABC 1"]),
                   ("conreq", ["PROTOCOLMODE ARQ", "ARQBW 500MAX", "ARQCALL K1ABC 1"])):
    manifest.append(run(kind, port, simple(kind, cmds)))
    port += 3
    print(kind, manifest[-1]["decoded"], flush=True)
json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w"), indent=1)
