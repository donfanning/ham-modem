"""Inside the Direwolf container: hub mix -> Direwolf's UDP audio input; Direwolf's transmit audio (ALSA file plugin -> FIFO
-> our stdin) -> hub.

Direwolf writes a whole transmission at once and nothing while idle; the hub expects one 20 ms block
(320 bytes of s16le at 8 kHz) every 20 ms from each participant, so this paces the output and fills idle time
with silence (which also keeps the hub from timing the participant out).
"""
import os
import select
import socket
import sys
import time

hub = (sys.argv[1], int(sys.argv[2]))
dw_in = ("127.0.0.1", 7355)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)
src = sys.stdin.buffer.fileno()
os.set_blocking(src, False)
pending = bytearray()
silence = bytes(320)
next_tick = time.monotonic()
while True:
    r, _, _ = select.select([sock, src], [], [], max(0.0, next_tick - time.monotonic()))
    if sock in r:
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except BlockingIOError:
                break
            sock.sendto(data, dw_in)
    if src in r:
        try:
            chunk = os.read(src, 65536)
        except BlockingIOError:
            chunk = None
        if chunk == b"":
            break                                   # Direwolf exited
        if chunk:
            pending.extend(chunk)
    now = time.monotonic()
    while now >= next_tick:
        if pending:
            block = bytes(pending[:320]).ljust(320, b"\0")
            del pending[:320]
        else:
            block = silence
        sock.sendto(block, hub)
        next_tick += 0.02
