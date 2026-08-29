import copy
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_ephemeris", ROOT / "scripts" / "build_ephemeris.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EphemerisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((ROOT / "tests" / "telemetry_fixture.json").read_text(encoding="utf-8"))

    def test_builds_valid_fixed_width_tles(self):
        orbit = MODULE.extract_orbit(self.payload, allow_stale=True)
        self.assertEqual(orbit.epoch, datetime(2026, 8, 29, 5, 33, 15, tzinfo=timezone.utc))
        latest, history = MODULE.render_files(orbit, "", "")
        lines = [line for line in latest.splitlines() if line.startswith(("1 ", "2 "))]
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertEqual(len(line), 69)
            self.assertEqual(MODULE.tle_checksum(line[:-1]), line)
        self.assertIn("26241.23142361", latest)
        self.assertIn("BY04-20260829-MEAN", history)

    def test_history_is_idempotent(self):
        orbit = MODULE.extract_orbit(self.payload, allow_stale=True)
        latest, history = MODULE.render_files(orbit, "", "")
        latest_again, history_again = MODULE.render_files(orbit, latest, history)
        self.assertEqual(latest_again, latest)
        self.assertEqual(history_again, history)

    def test_rejects_invalid_quality_flag(self):
        payload = copy.deepcopy(self.payload)
        payload["data"]["BY-04"]["轨道数据有效标识"]["f_double"] = 0
        with self.assertRaisesRegex(ValueError, "quality check failed"):
            MODULE.extract_orbit(payload, allow_stale=True)

if __name__ == "__main__":
    unittest.main()
