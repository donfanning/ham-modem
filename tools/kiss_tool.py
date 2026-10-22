#!/usr/bin/env python3
"""Tiny KISS host for testing a TNC over TCP or a serial/pty device.

    kiss_tool.py --tcp 127.0.0.1:8001 send N0CALL APRS "hello" [--path WIDE1-1] [--count 3]
    kiss_tool.py --dev ~/.ham-modem0 listen [--seconds 10]
"""

import argparse
import os
import select
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hammodem import kiss  # noqa: E402
from hammodem.ax25 import Ax25Error, Frame  # noqa: E402


def open_link(args):
    if args.tcp:
        host, _, port = args.tcp.rpartition(":")
        s = socket.create_connection((host or "127.0.0.1", int(port)))
        return s.fileno(), lambda d: s.sendall(d), lambda: s.recv(4096), s
    import tty  # noqa: PLC0415
    fd = os.open(os.path.expanduser(args.dev), os.O_RDWR | os.O_NOCTTY)
    tty.setraw(fd)
    return fd, lambda d: os.write(fd, d), lambda: os.read(fd, 4096), None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tcp")
    g.add_argument("--dev")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("src")
    s.add_argument("dest")
    s.add_argument("text")
    s.add_argument("--path", default="")
    s.add_argument("--count", type=int, default=1)
    l = sub.add_parser("listen")
    l.add_argument("--seconds", type=float, default=0)
    args = ap.parse_args()
    fd, write, read, keep = open_link(args)
    if args.cmd == "send":
        for i in range(args.count):
            text = args.text if args.count == 1 else f"{args.text} {i}"
            write(kiss.encode(Frame.ui(args.src, args.dest, text, args.path).encode()))
        time.sleep(0.2)
        return
    dec, end = kiss.Decoder(), time.monotonic() + args.seconds if args.seconds else None
    while end is None or time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        data = read()
        if not data:
            break
        for port, cmd, payload in dec.feed(data):
            if cmd == kiss.DATA:
                try:
                    print(Frame.decode(payload).monitor(), flush=True)
                except Ax25Error as exc:
                    print(f"<bad frame: {exc}>", flush=True)


if __name__ == "__main__":
    main()
