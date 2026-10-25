import unittest
import io
import os
import select
import subprocess
from unittest.mock import patch

import numpy as np

from hammodem.audio.device import AudioDeviceUnavailable, DeviceAudio, PulseAudio, audio_devices, format_audio_devices


class _FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeSoundDevice:
    default = type("Default", (), {"device": (0, 1)})

    def __init__(self):
        self.devices = [
            {"name": "Cable A monitor", "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 48000},
            {"name": "Cable B playback", "max_input_channels": 0, "max_output_channels": 1,
             "default_samplerate": 48000},
            {"name": "USB audio codec", "max_input_channels": 1, "max_output_channels": 1,
             "default_samplerate": 44100},
        ]
        self.stream = None

    def query_devices(self):
        return self.devices

    def Stream(self, **kwargs):
        self.stream = _FakeStream(**kwargs)
        return self.stream


class AudioDeviceListingTest(unittest.TestCase):
    def test_lists_input_output_and_defaults(self):
        sd = _FakeSoundDevice()
        rows = audio_devices(sd)
        self.assertEqual(rows[0]["name"], "Cable A monitor")
        self.assertTrue(rows[0]["default_input"])
        self.assertTrue(rows[1]["default_output"])
        self.assertIn("Cable B playback", format_audio_devices(sd))


class DeviceAudioTest(unittest.TestCase):
    def test_capture_queues_fixed_blocks_and_output_fills_callback(self):
        sd = _FakeSoundDevice()
        from unittest.mock import patch

        with patch("hammodem.audio.device._sounddevice", return_value=sd):
            audio = DeviceAudio("Cable A", "Cable B", fs=8000, frame_ms=20)
        self.assertTrue(sd.stream.started)
        self.assertEqual(sd.stream.kwargs["device"], (0, 1))
        self.assertEqual(sd.stream.kwargs["samplerate"], 8000)

        tx = np.arange(160, dtype=np.int16) - 80
        audio.send(tx)
        out_a = np.zeros((128, 1), np.float32)
        sd.stream.callback(np.zeros((128, 1), np.float32), out_a, 128, None, None)
        out_b = np.zeros((128, 1), np.float32)
        sd.stream.callback(np.arange(128, dtype=np.float32).reshape(-1, 1) / 128, out_b, 128, None, None)
        np.testing.assert_allclose(out_a[:, 0], tx[:128] / 32768.0)
        np.testing.assert_allclose(out_b[:32, 0], tx[128:] / 32768.0)
        np.testing.assert_array_equal(out_b[32:, 0], 0.0)

        blocks = audio.service([])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].shape, (160,))
        expected = np.concatenate((np.zeros(128, np.int16), np.arange(32, dtype=np.float32) / 128 * 32768))
        expected = np.clip(np.rint(expected), -32768, 32767).astype(np.int16)
        np.testing.assert_array_equal(blocks[0], expected)
        audio.close()
        self.assertTrue(sd.stream.closed)

    def test_name_must_resolve_unambiguously_and_have_requested_direction(self):
        from unittest.mock import patch

        sd = _FakeSoundDevice()
        with patch("hammodem.audio.device._sounddevice", return_value=sd):
            with self.assertRaises(AudioDeviceUnavailable):
                DeviceAudio("Cable", "Cable B", fs=8000)
            with self.assertRaises(AudioDeviceUnavailable):
                DeviceAudio("Cable A", "Cable A", fs=8000)

    def test_send_requires_one_modem_tick(self):
        from unittest.mock import patch

        sd = _FakeSoundDevice()
        with patch("hammodem.audio.device._sounddevice", return_value=sd):
            audio = DeviceAudio(0, 1, fs=8000)
        with self.assertRaises(ValueError):
            audio.send(np.zeros(80, np.int16))
        audio.close()


class PulseAudioTest(unittest.TestCase):
    def test_capture_fd_is_serviced_by_runtime_select_loop(self):
        read_fd, write_fd = os.pipe()
        capture_stdout = os.fdopen(read_fd, "rb", buffering=0)

        class FakeProcess:
            def __init__(self, stdout=None, stdin=None):
                self.stdout, self.stdin = stdout, stdin
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

        capture = FakeProcess(stdout=capture_stdout)
        playback = FakeProcess(stdin=io.BytesIO())
        with patch("hammodem.audio.device.pulse_devices", return_value=(
                ["test.monitor"], ["test.sink"])), \
             patch("hammodem.audio.device.subprocess.Popen", side_effect=(capture, playback)):
            audio = PulseAudio("test.monitor", "test.sink", fs=8000)
        try:
            samples = np.arange(160, dtype="<i2")
            os.write(write_fd, samples.tobytes())
            readable, _, _ = select.select(audio.fds(), [], [], 1.0)
            self.assertEqual(readable, [read_fd])
            blocks = audio.service(readable)
            self.assertEqual(len(blocks), 1)
            np.testing.assert_array_equal(blocks[0], samples)
        finally:
            audio.close()
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
