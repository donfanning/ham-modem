"""Inside the ardopcf container: UDP audio <-> ardopcf's ALSA FIFOs, in real time (stdlib only).

Every 20 ms the relay sends the peer one block (240 samples of S16_LE at 12 kHz) of ardopcf's transmit audio, or
silence while it is not transmitting. ardopcf generates a frame faster than real time (alsa_tap.c copies it here as
it is written); a 60 ms pre-roll per burst guards against the relay running dry inside a frame. In the other
direction it writes whatever blocks the peer sends into ardopcf's capture FIFO (silence if none arrived), so
ardopcf's capture runs at the real sample rate.

    python3 relay.py LISTEN_PORT PEER_HOST PEER_PORT
"""
import fcntl
import os
import socket
import sys
import time

F_SETPIPE_SZ = 1031
BLOCK = 480                                         # bytes: 240 samples = 20 ms at 12 kHz
PREROLL = 3 * BLOCK                                 # 60 ms
RX, TX = "/tmp/ardop_rx.fifo", "/tmp/ardop_tx.fifo"
for f in (RX, TX):
    if not os.path.exists(f):
        os.mkfifo(f)
rx = os.open(RX, os.O_RDWR | os.O_NONBLOCK)         # O_RDWR: never blocks on open, never sees EOF
tx = os.open(TX, os.O_RDWR | os.O_NONBLOCK)
fcntl.fcntl(rx, F_SETPIPE_SZ, 4096)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", int(sys.argv[1])))
sock.setblocking(False)
peer = (sys.argv[2], int(sys.argv[3]))
pending = bytearray()
inbox = []
silence = bytes(BLOCK)
next_tick = time.monotonic()
playing, first_seen = False, None
while True:
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            inbox.append(data)
        except BlockingIOError:
            break
    try:
        while True:
            chunk = os.read(tx, 65536)
            if not chunk:
                break
            pending.extend(chunk)
    except BlockingIOError:
        pass
    now = time.monotonic()
    if now < next_tick:
        time.sleep(min(0.005, next_tick - now))
        continue
    next_tick += 0.02
    if now - next_tick > 0.5:                       # fell far behind (stopped?): resynchronise
        next_tick = now + 0.02
    if not playing and pending:
        first_seen = first_seen or now
        playing = len(pending) >= PREROLL or now - first_seen > 0.3
    if playing:
        block = bytes(pending[:BLOCK]).ljust(BLOCK, b"\0")
        del pending[:BLOCK]
        if not pending:
            playing, first_seen = False, None
    else:
        block = silence
    sock.sendto(block, peer)
    data = inbox.pop(0) if inbox else silence
    if len(inbox) > 10:                             # peer ahead of us: drop backlog
        inbox.clear()
    try:
        os.write(rx, data[:BLOCK].ljust(BLOCK, b"\0"))
    except BlockingIOError:
        pass                                        # ardopcf not reading yet
