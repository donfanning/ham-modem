"""Command line tools for the HF image modes (WEFAX and SSTV).

    ham-image encode --format fax  --input chart.png --output chart.wav
    ham-image decode --format fax  --input chart.wav --output chart.png
    ham-image send   --format fax  --input chart.png --udp-audio 7401:127.0.0.1:7402
    ham-image receive --format fax --output chart.png --udp-audio 7401

The send/receive commands use the same raw-PCM UDP leg as ``ham-modem`` and
can also place a SIP call with ``--sip-dial`` or answer one with
``--sip-listen``.  A progress meter is written to stderr.
"""

from __future__ import annotations

import argparse
import select
import sys
import time
import wave
from pathlib import Path

import numpy as np

from .audio.rtp import CODECS
from .audio.sip import SipLeg
from .audio.udp import UdpAudio
from .image.fax import HfFax, HfFaxDecoder
from .image.sstv import SstvDecoder, SstvEncoder

FS = 8000


class Progress:
    """A small, dependency-free terminal progress meter."""

    def __init__(self, label: str, total: float | None = None, quiet: bool = False) -> None:
        self.label = label
        self.total = total
        self.quiet = quiet
        self._last = 0.0

    def update(self, done: float, total: float | None = None) -> None:
        if self.quiet:
            return
        total = total if total is not None else self.total
        now = time.monotonic()
        if now - self._last < 0.08 and (total is None or done < total):
            return
        self._last = now
        if total and total > 0:
            frac = max(0.0, min(1.0, done / total))
            width = 30
            bar = "#" * int(round(width * frac)) + "-" * (width - int(round(width * frac)))
            text = f"\r{self.label} [{bar}] {frac * 100:3.0f}%  {done:6.1f}/{total:.1f}s"
        else:
            text = f"\r{self.label} {done:6.1f}s received"
        sys.stderr.write(text)
        sys.stderr.flush()

    def finish(self, done: float, total: float | None = None) -> None:
        if self.quiet:
            return
        self.update(done, total)
        sys.stderr.write("\n")
        sys.stderr.flush()


def _load_image(path: str, fmt: str) -> np.ndarray:
    from PIL import Image  # noqa: PLC0415

    image = Image.open(path)
    return np.asarray(image.convert("L" if fmt == "fax" else "RGB"))


def _save_image(path: str, image: np.ndarray) -> None:
    from PIL import Image  # noqa: PLC0415

    Image.fromarray(image).save(path)


def _read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wav:
        channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
        raw = wav.readframes(frames)
    if width != 2:
        raise ValueError("WAV must be 16-bit PCM")
    audio = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return audio, rate


def _write_wav(path: str, audio: np.ndarray, fs: int = FS) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(fs)
        wav.writeframes(np.asarray(audio, dtype="<i2").tobytes())


def _encode_image(args) -> np.ndarray:
    image = _load_image(args.input, args.format)
    if args.format == "fax":
        return HfFax(lpm=args.lpm, ioc=args.ioc).encode(image)
    return SstvEncoder(mode=args.sstv_mode).encode(image)


def _decode_audio(args, audio: np.ndarray) -> np.ndarray | None:
    if args.format == "fax":
        return HfFaxDecoder(lpm=args.lpm, ioc=args.ioc).decode(audio)
    return SstvDecoder().decode(audio)


def _hostport(text: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return (host or default_host), int(port)


def _make_leg(args, parser: argparse.ArgumentParser, sending: bool):
    if args.udp_audio:
        lport, _, peer = args.udp_audio.partition(":")
        if sending:
            if not peer:
                parser.error("send needs --udp-audio LPORT:HOST:PORT")
            return UdpAudio(("0.0.0.0", int(lport)), _hostport(peer), FS)
        return UdpAudio(("0.0.0.0", int(lport)), None, FS)
    if args.sip_dial or args.sip_listen:
        if sending and not args.sip_dial:
            parser.error("SIP send needs --sip-dial")
        if not sending and not args.sip_listen:
            parser.error("SIP receive needs --sip-listen")
        return SipLeg(
            dial=args.sip_dial, listen=args.sip_listen,
            sip_port=args.sip_port, media_port=args.media_port,
            local_ip=args.local_ip, user=args.sip_user,
            password=args.sip_password, codecs=[CODECS["pcmu"]],
        )
    parser.error("choose --udp-audio or --sip-dial/--sip-listen")
    raise AssertionError("unreachable")


def _service(leg, timeout: float = 0.0) -> list[np.ndarray]:
    try:
        readable, _, _ = select.select(leg.fds(), [], [], timeout)
    except InterruptedError:
        return []
    return leg.service(readable) if readable else []


def _wait_up(leg, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while getattr(leg, "state", "up") != "up":
        if time.monotonic() > deadline:
            return False
        _service(leg, 0.1)
    return True


def _send(args, parser) -> int:
    audio = _encode_image(args)
    lead = np.zeros(int(FS * args.lead), dtype=np.int16)
    tail = np.zeros(int(FS * args.tail), dtype=np.int16)
    audio = np.concatenate((lead, np.asarray(audio, dtype=np.int16), tail))
    total = len(audio) / FS
    leg = _make_leg(args, parser, sending=True)
    progress = Progress("send", total, args.quiet)
    try:
        if not _wait_up(leg, args.timeout):
            print("ham-image: link did not come up", file=sys.stderr)
            return 1
        chunk = leg.frame_samples
        next_tx = time.monotonic()
        sent = 0
        while sent < len(audio):
            now = time.monotonic()
            if now < next_tx:
                time.sleep(min(next_tx - now, 0.05))
                continue
            block = audio[sent:sent + chunk]
            leg.send(block)
            sent += len(block)
            _service(leg, 0.0)
            next_tx += len(block) / FS
            progress.update(sent / FS, total)
        progress.finish(total, total)
    finally:
        leg.close()
    return 0


def _receive(args, parser) -> int:
    leg = _make_leg(args, parser, sending=False)
    progress = Progress("receive", args.duration or None, args.quiet)
    chunks: list[np.ndarray] = []
    start = time.monotonic()
    last_audio: float | None = None
    try:
        if not _wait_up(leg, args.timeout):
            print("ham-image: no incoming call", file=sys.stderr)
            return 1
        while True:
            blocks = _service(leg, 0.15)
            now = time.monotonic()
            if blocks:
                chunks.extend(np.asarray(b, dtype=np.int16) for b in blocks)
                last_audio = now
            elapsed = now - start
            progress.update(elapsed, args.duration or None)
            if args.duration and elapsed >= args.duration:
                break
            if last_audio is not None and now - last_audio >= args.silence:
                break
            if last_audio is None and now - start >= args.timeout:
                break
        progress.finish(time.monotonic() - start, args.duration or None)
    finally:
        leg.close()

    if not chunks:
        print("ham-image: no audio received", file=sys.stderr)
        return 1
    audio = np.concatenate(chunks)
    image = _decode_audio(args, audio)
    if image is None:
        print("ham-image: could not decode the received audio", file=sys.stderr)
        return 1
    _save_image(args.output, image)
    print(f"wrote {args.output} ({image.shape[1]}x{image.shape[0]})", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ham-image", description="WEFAX and SSTV image tools")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, *, input_required=False, output_required=False):
        p.add_argument("--format", choices=("fax", "sstv"), default="fax")
        if input_required:
            p.add_argument("--input", required=True, help="input image or WAV")
        if output_required:
            p.add_argument("--output", required=True, help="output WAV or image")
        p.add_argument("--lpm", type=int, choices=(60, 120), default=120, help="FAX lines per minute")
        p.add_argument("--ioc", type=int, choices=(288, 576), default=576, help="FAX index of cooperation")
        p.add_argument("--sstv-mode", default="robot36", choices=SstvEncoder.MODES,
                       help="SSTV mode")

    p = sub.add_parser("encode", help="image -> WAV")
    common(p, input_required=True, output_required=True)
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("decode", help="WAV -> image")
    common(p, input_required=True, output_required=True)
    p.add_argument("--quiet", action="store_true")

    for name, sending in (("send", True), ("receive", False)):
        p = sub.add_parser(name, help="live image transfer")
        common(p, input_required=sending, output_required=not sending)
        p.add_argument("--udp-audio", metavar="LPORT[:HOST:PORT]",
                       help="raw PCM UDP leg (send: LPORT:HOST:PORT; receive: LPORT)")
        p.add_argument("--sip-dial", metavar="EXT@HOST[:PORT]")
        p.add_argument("--sip-listen", action="store_true")
        p.add_argument("--sip-port", type=int, default=0)
        p.add_argument("--media-port", type=int, default=0)
        p.add_argument("--local-ip")
        p.add_argument("--sip-user")
        p.add_argument("--sip-password")
        p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for a link/call")
        p.add_argument("--duration", type=float, default=0.0,
                       help="maximum receive time (0 = until the line goes quiet)")
        p.add_argument("--silence", type=float, default=2.0, help="stop receive after this much silence")
        p.add_argument("--lead", type=float, default=0.3, help="seconds of lead-in silence to send")
        p.add_argument("--tail", type=float, default=0.5, help="seconds of tail silence to send")
        p.add_argument("--quiet", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "encode":
        audio = _encode_image(args)
        _write_wav(args.output, audio)
        print(f"wrote {args.output} ({len(audio) / FS:.1f}s)", file=sys.stderr)
        return
    if args.command == "decode":
        audio, rate = _read_wav(args.input)
        if rate != FS:
            print(f"ham-image: WAV is {rate} Hz; image modes expect {FS} Hz", file=sys.stderr)
            raise SystemExit(1)
        image = _decode_audio(args, audio)
        if image is None:
            print("ham-image: could not decode the WAV", file=sys.stderr)
            raise SystemExit(1)
        _save_image(args.output, image)
        print(f"wrote {args.output} ({image.shape[1]}x{image.shape[0]})", file=sys.stderr)
        return
    if args.command == "send":
        raise SystemExit(_send(args, parser))
    if args.command == "receive":
        raise SystemExit(_receive(args, parser))


if __name__ == "__main__":
    main()
