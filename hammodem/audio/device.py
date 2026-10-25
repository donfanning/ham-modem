"""Full-duplex audio through PortAudio or PulseAudio devices (including OS virtual audio cables).

The stream runs at the modem's sample rate and presents the same block interface as the UDP/SIP legs. PortAudio
callbacks only move samples through bounded queues; modem processing stays in the main TNC thread.
"""

from __future__ import annotations

import queue
import os
import shutil
import subprocess
import threading
from typing import Any

import numpy as np


class AudioDeviceUnavailable(RuntimeError):
    """The optional system audio backend is missing or could not open the requested devices."""


def _sounddevice():
    try:
        import sounddevice as sd  # noqa: PLC0415
    except (ImportError, OSError) as exc:
        raise AudioDeviceUnavailable(
            "system audio requires the optional sounddevice dependency and PortAudio; install with "
            "`pip install -e '.[audio]'` and install your OS PortAudio runtime (for example, libportaudio2 on Debian/Ubuntu)"
        ) from exc
    return sd


def audio_devices(sd: Any | None = None) -> list[dict[str, Any]]:
    """Return available input/output devices in a stable, printable form."""
    sd = sd or _sounddevice()
    default_in, default_out = sd.default.device
    result = []
    for index, raw in enumerate(sd.query_devices()):
        result.append({
            "index": index,
            "name": str(raw["name"]),
            "inputs": int(raw["max_input_channels"]),
            "outputs": int(raw["max_output_channels"]),
            "rate": int(float(raw["default_samplerate"])),
            "default_input": index == default_in,
            "default_output": index == default_out,
        })
    return result


def format_audio_devices(sd: Any | None = None) -> str:
    rows = audio_devices(sd)
    if not rows:
        return "No audio devices found."
    out = ["Index  In  Out  Default Hz  Device"]
    for row in rows:
        marker = ("*" if row["default_input"] else " ") + ("*" if row["default_output"] else " ")
        out.append(f"{row['index']:>5}  {row['inputs']:>2}  {row['outputs']:>3}  {marker:>7}  "
                   f"{row['rate']:>9}  {row['name']}")
    out.append("Default markers: first * = input, second * = output. Select a device by index or name.")
    return "\n".join(out)


def pulse_available() -> bool:
    return all(shutil.which(x) for x in ("pactl", "parec", "pacat")) and \
        subprocess.run(["pactl", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def pulse_devices() -> tuple[list[str], list[str]]:
    """Return PulseAudio source and sink names, including PipeWire's Pulse compatibility endpoints."""
    if not pulse_available():
        raise AudioDeviceUnavailable("PulseAudio/PipeWire tools or server are unavailable (need pactl, parec, pacat)")
    def names(kind):
        result = subprocess.run(["pactl", "list", "short", kind], check=True, capture_output=True, text=True)
        return [line.split("\t")[1] for line in result.stdout.splitlines() if len(line.split("\t")) > 1]
    return names("sources"), names("sinks")


def format_all_audio_devices() -> str:
    sections = []
    try:
        sources, sinks = pulse_devices()
        sections.append("PulseAudio/PipeWire capture sources:\n" + "\n".join(f"  {x}" for x in sources)
                        + "\nPlayback sinks:\n" + "\n".join(f"  {x}" for x in sinks))
    except (AudioDeviceUnavailable, subprocess.SubprocessError):
        pass
    try:
        sections.append("PortAudio devices:\n" + format_audio_devices())
    except AudioDeviceUnavailable as exc:
        sections.append(f"PortAudio unavailable: {exc}")
    return "\n\n".join(sections) if sections else "No audio devices found."


def _pulse_resolve(selector: str, names: list[str], direction: str) -> str:
    text = selector.strip()
    exact = [name for name in names if name.casefold() == text.casefold()]
    matches = exact or [name for name in names if text.casefold() in name.casefold()]
    if not matches:
        raise AudioDeviceUnavailable(f"no PulseAudio {direction} matches {text!r}; use --list-audio-devices")
    if len(matches) > 1:
        raise AudioDeviceUnavailable(f"PulseAudio {direction} selector {text!r} is ambiguous: {', '.join(matches)}")
    return matches[0]


class PulseAudio:
    """PulseAudio/PipeWire full-duplex PCM leg using native server tools, no PortAudio dependency."""
    def __init__(self, input_device: str, output_device: str, fs: int = 8000,
                 frame_ms: int = 20, queue_blocks: int = 50) -> None:
        if fs <= 0 or frame_ms <= 0 or fs * frame_ms % 1000:
            raise ValueError("fs * frame_ms must be a positive whole number of samples")
        if queue_blocks < 2:
            raise ValueError("queue_blocks must be at least 2")
        sources, sinks = pulse_devices()
        self.input_name = _pulse_resolve(input_device, sources, "capture source")
        self.output_name = _pulse_resolve(output_device, sinks, "playback sink")
        self.rate = fs
        self.frame_samples = fs * frame_ms // 1000
        self.name = f"pulse:{self.input_name} -> {self.output_name} ({fs} Hz mono)"
        self._rx: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_blocks)
        self._tx: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_blocks)
        self._rx_pending = bytearray()
        self.rx_overflows = self.rx_queue_drops = self.tx_underflows = self.tx_queue_drops = self.callback_errors = 0
        args = ["--raw", "--format=s16le", f"--rate={fs}", "--channels=1", "--latency-msec=20"]
        self._capture = None
        try:
            self._capture = subprocess.Popen(["parec", *args, f"--device={self.input_name}"],
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._playback = subprocess.Popen(["pacat", "--playback", *args, f"--device={self.output_name}"],
                                              stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as exc:
            if self._capture is not None:
                self._capture.terminate()
                self._capture.wait()
            raise AudioDeviceUnavailable(f"could not start PulseAudio streams: {exc}") from exc
        if self._capture.stdout is None:
            self._capture.terminate()
            self._playback.terminate()
            raise AudioDeviceUnavailable("could not open PulseAudio capture pipe")
        os.set_blocking(self._capture.stdout.fileno(), False)
        self._closed = threading.Event()
        self._writer = threading.Thread(target=self._write_playback, daemon=True, name="ham-modem-pulse-tx")
        self._writer.start()

    def _write_playback(self):
        while not self._closed.is_set():
            try:
                block = self._tx.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if self._playback.stdin:
                    self._playback.stdin.write(block.astype("<i2", copy=False).tobytes())
                    self._playback.stdin.flush()
            except (BrokenPipeError, OSError):
                self.callback_errors += 1
                break

    def fds(self):
        return [self._capture.stdout.fileno()] if self._capture.stdout else []

    def service(self, readable):
        for proc, direction in ((self._capture, "capture"), (self._playback, "playback")):
            if proc.poll() is not None and not self._closed.is_set():
                raise AudioDeviceUnavailable(f"PulseAudio {direction} process exited with status {proc.returncode}")
        fd = self._capture.stdout.fileno() if self._capture.stdout else None
        if fd in readable:
            frame_bytes = self.frame_samples * 2
            try:
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    self._rx_pending.extend(data)
            except BlockingIOError:
                pass
            except OSError:
                self.callback_errors += 1
            while len(self._rx_pending) >= frame_bytes:
                raw = bytes(self._rx_pending[:frame_bytes])
                del self._rx_pending[:frame_bytes]
                try:
                    self._rx.put_nowait(np.frombuffer(raw, dtype="<i2").copy())
                except queue.Full:
                    self.rx_queue_drops += 1
        blocks = []
        while True:
            try:
                blocks.append(self._rx.get_nowait())
            except queue.Empty:
                return blocks

    def send(self, pcm):
        block = np.asarray(pcm, dtype=np.int16).reshape(-1)
        if len(block) != self.frame_samples:
            raise ValueError(f"audio output block must contain {self.frame_samples} samples, got {len(block)}")
        try:
            self._tx.put_nowait(block.copy())
        except queue.Full:
            try:
                self._tx.get_nowait()
            except queue.Empty:
                pass
            self.tx_queue_drops += 1
            try:
                self._tx.put_nowait(block.copy())
            except queue.Full:
                self.tx_queue_drops += 1

    def close(self):
        self._closed.set()
        for proc in (self._capture, self._playback):
            try:
                if proc.stdin:
                    proc.stdin.close()
                if proc.stdout:
                    proc.stdout.close()
                proc.terminate()
            except OSError:
                pass
        self._writer.join(timeout=1)
        for proc in (self._capture, self._playback):
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()


def _resolve_device(sd: Any, selector: str | int, kind: str) -> tuple[int, str]:
    if kind not in ("input", "output"):
        raise ValueError(f"invalid audio direction {kind!r}")
    key = f"max_{kind}_channels"
    devices = list(sd.query_devices())
    text = str(selector).strip()
    matches: list[tuple[int, dict]]
    if text.isdecimal():
        index = int(text)
        if index < 0 or index >= len(devices):
            raise AudioDeviceUnavailable(f"audio device index {index} does not exist; use --list-audio-devices")
        matches = [(index, devices[index])]
    else:
        folded = text.casefold()
        exact = [(i, d) for i, d in enumerate(devices) if str(d["name"]).casefold() == folded]
        matches = exact or [(i, d) for i, d in enumerate(devices) if folded in str(d["name"]).casefold()]
        if not matches:
            raise AudioDeviceUnavailable(f"no audio {kind} device matches {text!r}; use --list-audio-devices")
        if len(matches) > 1:
            choices = ", ".join(f"{i}: {d['name']}" for i, d in matches)
            raise AudioDeviceUnavailable(f"audio device name {text!r} is ambiguous ({choices}); use an index")
    index, device = matches[0]
    if int(device[key]) < 1:
        raise AudioDeviceUnavailable(f"audio device {index} ({device['name']}) has no {kind} channels")
    return index, str(device["name"])


class DeviceAudio:
    """Full-duplex mono PCM audio leg backed by a PortAudio input/output stream.

    ``input_device`` and ``output_device`` are device indexes or exact/unique name substrings from
    :func:`audio_devices`. Capture and playback are mono signed 16-bit PCM at ``fs`` to the modem. The host audio
    API may resample this stream to its device's native rate.
    """

    def __init__(self, input_device: str | int, output_device: str | int, fs: int = 8000,
                 frame_ms: int = 20, queue_blocks: int = 50, latency: str | float = "low") -> None:
        if fs <= 0 or frame_ms <= 0 or fs * frame_ms % 1000:
            raise ValueError("fs * frame_ms must be a positive whole number of samples")
        if queue_blocks < 2:
            raise ValueError("queue_blocks must be at least 2")
        self.sd = _sounddevice()
        self.input_index, self.input_name = _resolve_device(self.sd, input_device, "input")
        self.output_index, self.output_name = _resolve_device(self.sd, output_device, "output")
        self.rate = int(fs)
        self.frame_samples = self.rate * frame_ms // 1000
        self.name = f"audio:{self.input_name} -> {self.output_name} ({self.rate} Hz mono)"
        self._rx_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_blocks)
        self._tx_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_blocks)
        self._rx_remainder = np.empty(0, dtype=np.int16)
        self._tx_pending = np.empty(0, dtype=np.float32)
        self._tx_offset = 0
        self.rx_overflows = 0
        self.rx_queue_drops = 0
        self.tx_underflows = 0
        self.tx_queue_drops = 0
        self.callback_errors = 0

        def callback(indata, outdata, frames, time_info, status):
            del time_info
            if status:
                if getattr(status, "input_overflow", False):
                    self.rx_overflows += 1
                if getattr(status, "output_underflow", False):
                    self.tx_underflows += 1
            try:
                captured = np.clip(indata[:, 0] * 32768.0, -32768, 32767).astype(np.int16)
                try:
                    self._rx_queue.put_nowait(captured)
                except queue.Full:
                    self.rx_queue_drops += 1

                outdata.fill(0.0)
                written = 0
                while written < frames:
                    if self._tx_offset >= len(self._tx_pending):
                        try:
                            self._tx_pending = self._tx_queue.get_nowait()
                        except queue.Empty:
                            break
                        self._tx_offset = 0
                    count = min(frames - written, len(self._tx_pending) - self._tx_offset)
                    outdata[written:written + count, 0] = \
                        self._tx_pending[self._tx_offset:self._tx_offset + count, 0]
                    self._tx_offset += count
                    written += count
                if written < frames:
                    self.tx_underflows += 1
            except Exception:
                self.callback_errors += 1
                outdata.fill(0.0)

        try:
            self.stream = self.sd.Stream(
                device=(self.input_index, self.output_index),
                samplerate=self.rate,
                blocksize=0,
                channels=(1, 1),
                dtype="float32",
                latency=latency,
                callback=callback,
            )
            self.stream.start()
        except Exception as exc:
            raise AudioDeviceUnavailable(
                f"could not open audio stream at {self.rate} Hz: {exc}; check device availability and sample-rate support"
            ) from exc

    def fds(self):
        # PortAudio owns its callback thread. Runtime's regular audio tick calls service() to drain captured blocks.
        return []

    def service(self, readable) -> list[np.ndarray]:
        del readable
        chunks = []
        while True:
            try:
                chunks.append(self._rx_queue.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return []
        if self._rx_remainder.size:
            chunks.insert(0, self._rx_remainder)
        joined = np.concatenate(chunks)
        count = len(joined) // self.frame_samples
        if count == 0:
            self._rx_remainder = joined
            return []
        blocks = [joined[i * self.frame_samples:(i + 1) * self.frame_samples].copy() for i in range(count)]
        self._rx_remainder = joined[count * self.frame_samples:].copy()
        return blocks

    def send(self, pcm: np.ndarray) -> None:
        samples = np.asarray(pcm, dtype=np.int16).reshape(-1)
        if len(samples) != self.frame_samples:
            raise ValueError(f"audio output block must contain {self.frame_samples} samples, got {len(samples)}")
        block = (samples.astype(np.float32) / 32768.0).reshape(-1, 1)
        try:
            self._tx_queue.put_nowait(block)
        except queue.Full:
            try:
                self._tx_queue.get_nowait()
            except queue.Empty:
                pass
            self.tx_queue_drops += 1
            try:
                self._tx_queue.put_nowait(block)
            except queue.Full:
                self.tx_queue_drops += 1

    def close(self) -> None:
        stream = getattr(self, "stream", None)
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
