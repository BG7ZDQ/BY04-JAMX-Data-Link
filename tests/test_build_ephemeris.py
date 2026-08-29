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
        cls.orbit = MODULE.extract_orbit(cls.payload, allow_stale=True)
        cls.fitted = MODULE.fit_sgp4(cls.orbit)

    def test_uses_true_utc_epoch(self):
        self.assertEqual(self.orbit.epoch, datetime(2026, 8, 29, 13, 33, 15, tzinfo=timezone.utc))

    def test_fit_converges_to_telemetry_state(self):
        self.assertLess(self.fitted.position_error_m, 10.0)
        self.assertLess(self.fitted.velocity_error_m_s, 0.05)

    def test_builds_valid_and_accurate_tles(self):
        latest, history, residual = MODULE.render_files(self.orbit, self.fitted, "", "")
        lines = [line for line in latest.splitlines() if line.startswith(("1 ", "2 "))]
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertEqual(len(line), 69)
            self.assertEqual(MODULE.tle_checksum(line[:-1]), line)
        self.assertLess(residual[0], MODULE.MAX_POSITION_ERROR_M)
        self.assertLess(residual[1], MODULE.MAX_VELOCITY_ERROR_M_S)
        self.assertIn("26241.56475694", latest)
        self.assertIn("BY04-20260829-MEAN", history)

    def test_history_replaces_same_day_and_is_idempotent(self):
        latest, history, _ = MODULE.render_files(self.orbit, self.fitted, "", "")
        latest_again, history_again, _ = MODULE.render_files(self.orbit, self.fitted, latest, history)
        self.assertEqual(latest_again, latest)
        self.assertEqual(history_again, history)
        self.assertEqual(history.count("BY04-20260829-MEAN"), 1)

    def test_rejects_invalid_quality_flag(self):
        payload = copy.deepcopy(self.payload)
        payload["data"]["BY-04"]["轨道数据有效标识"]["f_double"] = 0
        with self.assertRaisesRegex(ValueError, "quality check failed"):
            MODULE.extract_orbit(payload, allow_stale=True)


if __name__ == "__main__":
    unittest.main()
