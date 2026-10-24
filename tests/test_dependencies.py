"""Packaging metadata must match the numpy APIs the code uses."""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NumpyFloorTest(unittest.TestCase):
    def test_numpy_floor_covers_bitwise_count(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        deps = data["project"]["dependencies"]
        spec = next(d for d in deps if d.lower().startswith("numpy"))
        major = int(re.search(r"(\d+)", spec).group(1))
        # numpy.bitwise_count (il2p.py, fx25.py) was added in numpy 2.0.
        self.assertGreaterEqual(major, 2, f"{spec} is too old for numpy.bitwise_count")


if __name__ == "__main__":
    unittest.main()
