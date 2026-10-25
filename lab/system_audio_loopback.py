#!/usr/bin/env python3
"""Exercise the system audio leg through cross-connected PulseAudio/PipeWire null sinks.

Creates temporary sinks and tests AFSK1200 plus ARDOP 500 Hz ARQ in each direction. This is a software audio
loopback test; it does not model a radio, acoustic/electrical interface, PTT, or an RF channel.
"""

from __future__ import annotations

import argparse
import select
import subprocess
import time
import uuid

import numpy as np

from hammodem import kiss
from hammodem.ax25 import Frame
from hammodem.audio.device import PulseAudio
from hammodem.ardop.arq import ArqStation
from hammodem.ardop.tnc import ArdopTnc
from hammodem.tnc import Tnc


def tick_loop(audio_a, audio_b, step_a, step_b, done, timeout: float) -> tuple[bool, int]:
    """Clock two stations through independent audio legs as the application runtime does."""
    n = audio_a.frame_samples
    tick = n / 8000
    next_tx = time.monotonic()
    deadline = next_tx + timeout
    blocks = 0
    while time.monotonic() < deadline and not done():
        now = time.monotonic()
        while now >= next_tx:
            step_a.tx_tick(n)
            step_b.tx_tick(n)
            next_tx += tick
        readable, _, _ = select.select(audio_a.fds() + audio_b.fds(), [], [], max(0.0, next_tx - time.monotonic()))
        for block in audio_a.service(readable):
            step_a.rx_block(block)
            blocks += 1
        for block in audio_b.service(readable):
            step_b.rx_block(block)
            blocks += 1
        now = time.monotonic()
        if now - next_tx > 0.2:
            next_tx = now
    return done(), blocks


class PacketStep:
    def __init__(self, tnc: Tnc, audio: PulseAudio) -> None:
        self.tnc, self.audio = tnc, audio

    def tx_tick(self, n: int) -> None:
        self.tnc.tick()
        self.audio.send(self.tnc.audio_out(n))

    def rx_block(self, block: np.ndarray) -> None:
        self.tnc.audio_in(block)


class ArdopStep:
    def __init__(self, tnc: ArdopTnc, audio: PulseAudio) -> None:
        self.tnc, self.audio = tnc, audio

    def tx_tick(self, n: int) -> None:
        self.audio.send(self.tnc.audio_out(n))

    def rx_block(self, block: np.ndarray) -> None:
        self.tnc.audio_in(block)


def crossconnect(prefix: str, rate: int) -> tuple[int, int, str, str]:
    a, b = f"{prefix}_a", f"{prefix}_b"
    module_a = int(subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={a}",
                                   f"rate={rate}", "channels=1"], check=True, capture_output=True, text=True).stdout)
    try:
        module_b = int(subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={b}",
                                       f"rate={rate}", "channels=1"], check=True, capture_output=True, text=True).stdout)
    except Exception:
        subprocess.run(["pactl", "unload-module", str(module_a)], check=False)
        raise
    return module_a, module_b, a, b


def run_afsk(sink_a: str, sink_b: str, timeout: float) -> dict:
    audio_a = PulseAudio(f"{sink_b}.monitor", sink_a, 8000)
    audio_b = PulseAudio(f"{sink_a}.monitor", sink_b, 8000)
    try:
        tnc_a = Tnc("afsk1200", 8000, txdelay=0.08, slottime=0.02, persist=255)
        tnc_b = Tnc("afsk1200", 8000, txdelay=0.08, slottime=0.02, persist=255)
        wire = bytearray()
        tnc_b.attach(wire.extend)
        decoder = kiss.Decoder()
        received = []
        wire_offset = 0
        expected = Frame.ui("N0CALL", "APRS", ">virtual audio loopback", "").encode()
        tnc_a.send(expected)
        def got_frame():
            nonlocal wire_offset
            raw = bytes(wire)
            received.extend(d for _, cmd, d in decoder.feed(raw[wire_offset:]) if cmd == kiss.DATA)
            wire_offset = len(raw)
            return expected in received
        ok, blocks = tick_loop(audio_a, audio_b, PacketStep(tnc_a, audio_a), PacketStep(tnc_b, audio_b),
                               got_frame, timeout)
        return {"mode": "AFSK1200", "delivered": ok and expected in received, "heard": tnc_b.heard,
                "audio_blocks": blocks, "queue_drops": [audio_a.rx_queue_drops, audio_a.tx_queue_drops,
                                                         audio_b.rx_queue_drops, audio_b.tx_queue_drops]}
    finally:
        audio_a.close()
        audio_b.close()


def run_ardop_direction(sink_a: str, sink_b: str, caller: str, callee: str, data: bytes,
                        timeout: float) -> dict:
    audio_a = PulseAudio(f"{sink_b}.monitor", sink_a, 8000)
    audio_b = PulseAudio(f"{sink_a}.monitor", sink_b, 8000)
    try:
        station_a = ArqStation(caller, fs=8000, bandwidth="500MAX", seed=1)
        station_b = ArqStation(callee, fs=8000, bandwidth="500MAX", seed=2)
        tnc_a, tnc_b = ArdopTnc(station_a), ArdopTnc(station_b)
        station_a.send(data)
        station_a.connect(callee)
        ok, blocks = tick_loop(audio_a, audio_b, ArdopStep(tnc_a, audio_a), ArdopStep(tnc_b, audio_b),
                               lambda: bytes(station_b.received) == data, timeout)
        ack = any(row[1].startswith("DataACK") and row[2] for row in station_a.trace)
        station_a.abort()
        station_b.abort()
        # ARQ may still have queued repeats after the peer has accepted a data frame. Stop both modems and
        # service a short silence tail before opening the next direction.
        tick_loop(audio_a, audio_b, ArdopStep(tnc_a, audio_a), ArdopStep(tnc_b, audio_b), lambda: False, 0.6)
        return {"mode": "ARDOP 500MAX at 8 kHz", "direction": f"{caller}>{callee}",
                "delivered": ok and bytes(station_b.received) == data, "dataack_decoded": ack,
                "seconds": round(station_a.now / 1000, 2), "audio_blocks": blocks,
                "queue_drops": [audio_a.rx_queue_drops, audio_a.tx_queue_drops,
                                audio_b.rx_queue_drops, audio_b.tx_queue_drops],
                "trace": [row[1] for row in station_a.trace]}
    finally:
        audio_a.close()
        audio_b.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=50, help="seconds allowed per ARDOP direction (default 50)")
    args = parser.parse_args()
    prefix = f"hammodem_{uuid.uuid4().hex[:8]}"
    results = []
    tests = (
        ("afsk", lambda a, b: run_afsk(a, b, min(args.timeout, 10))),
        ("ardop_forward", lambda a, b: run_ardop_direction(
            a, b, "N0CALL", "K1ABC", b"ARDOP virtual audio forward 73", args.timeout)),
        ("ardop_return", lambda a, b: run_ardop_direction(
            a, b, "K1ABC", "N0CALL", b"ARDOP virtual audio return 73", args.timeout)),
    )
    for suffix, test in tests:
        modules: tuple[int, int] = (0, 0)
        try:
            module_a, module_b, sink_a, sink_b = crossconnect(f"{prefix}_{suffix}", 8000)
            modules = (module_a, module_b)
            results.append(test(sink_a, sink_b))
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            print(f"{suffix} loopback failed: {exc}")
            return_code = 2
            break
        finally:
            for module in modules:
                if module:
                    subprocess.run(["pactl", "unload-module", str(module)], check=False)
    else:
        return_code = 0
    if return_code:
        return return_code
    for result in results:
        print(result)
    success = all(x["delivered"] and not any(x["queue_drops"]) for x in results)
    success &= all(x.get("dataack_decoded", True) for x in results)
    print("PASS" if success else "FAIL")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
