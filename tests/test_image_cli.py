"""Offline tests for the ham-image command line tools."""

import contextlib
import io
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from hammodem.image_cli import main


class ImageCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _image(self, name, size, color=None):
        path = self.dir / name
        if color is None:
            data = np.zeros((size[1], size[0]), np.uint8)
            data[:, : size[0] // 2] = 255
        else:
            data = np.zeros((size[1], size[0], 3), np.uint8)
            data[:, : size[0] // 2] = color
        Image.fromarray(data).save(path)
        return path

    def test_fax_encode_decode_round_trip(self):
        source = self._image("fax.png", (64, 16))
        wav = self.dir / "fax.wav"
        output = self.dir / "fax_out.png"
        main(["encode", "--format", "fax", "--input", str(source), "--output", str(wav)])
        with wave.open(str(wav), "rb") as f:
            self.assertEqual(f.getframerate(), 8000)
            self.assertEqual(f.getnchannels(), 1)
            self.assertEqual(f.getsampwidth(), 2)
            self.assertGreater(f.getnframes(), 8000)
        main(["decode", "--format", "fax", "--input", str(wav), "--output", str(output)])
        self.assertTrue(output.exists())
        with Image.open(output) as img:
            self.assertEqual(img.size[1], 16)

    def test_sstv_encode_decode_round_trip(self):
        source = self._image("sstv.png", (160, 120), color=(255, 0, 0))
        wav = self.dir / "sstv.wav"
        output = self.dir / "sstv_out.png"
        main(["encode", "--format", "sstv", "--sstv-mode", "robot36",
              "--input", str(source), "--output", str(wav)])
        main(["decode", "--format", "sstv", "--input", str(wav), "--output", str(output)])
        self.assertTrue(output.exists())
        with Image.open(output) as img:
            self.assertEqual(img.size, (320, 240))

    def test_bad_wav_is_a_clean_error(self):
        bad = self.dir / "bad.wav"
        bad.write_bytes(b"not a wav")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((wave.Error, EOFError)):
                main(["decode", "--format", "fax", "--input", str(bad), "--output", str(self.dir / "x.png")])

    def test_help_lists_all_commands(self):
        from hammodem.image_cli import build_parser  # noqa: PLC0415

        parser = build_parser()
        text = parser.format_help()
        for command in ("encode", "decode", "send", "receive"):
            self.assertIn(command, text)


if __name__ == "__main__":
    unittest.main()
