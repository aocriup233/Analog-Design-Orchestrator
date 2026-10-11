import tempfile
import unittest
from pathlib import Path

from analog_agent.parameters import validate_value, validate_values, number_value
from analog_agent.netlist_agent import _render
from analog_agent.campaign import _validate_points
from analog_agent.core import create_run, write_json


class ParameterTest(unittest.TestCase):
    def test_spectre_scale_factors_are_case_sensitive(self):
        factors = {"T": 1e12, "G": 1e9, "M": 1e6, "K": 1e3, "k": 1e3,
                   "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}
        for suffix, expected in factors.items():
            with self.subTest(suffix=suffix):
                self.assertEqual(number_value("1" + suffix), expected)
        self.assertGreater(number_value("1M"), number_value("1k"))
        self.assertLess(number_value("1m"), number_value("1"))
        for suffix in ("t", "g", "U", "N", "P", "F", "A", "meg", "MEG"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                number_value("1" + suffix)

    def test_case_sensitive_bounds_and_rendered_value(self):
        schema = {"type": "number", "min": "1k", "max": "2M"}
        self.assertEqual(validate_value("R", "1M", schema), "1M")
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_value("R", "1m", schema)
        self.assertEqual(validate_value("R", "1m", {"type": "number", "min": "1m", "max": "1"}), "1m")
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_value("R", "1M", {"type": "number", "max": "1k"})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "template.scs"
            source.write_text("parameters R=@@R@@")
            self.assertEqual(_render(source, {"R": "1M"}, {"R": schema}), "parameters R=1M")

    def test_injections_all_entry_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "template.scs"
            source.write_text("parameters R=@@R@@")
            cfg = {"template": source.name, "parameters": {"R": {"initial": "1k"}},
                   "metrics": {"dc": {}}, "rules": {"dc": {}}}
            write_json(root / "project.json", cfg)
            manifest = {"cycles": [], "policy": {"max_points_per_cycle": 2,
                                                  "max_cycles": 2, "max_total_points": 4}}
            for value in ("1 include", '"1"', "1\\\ninclude", "1//comment", "include", "1\n", "1/*x*/"):
                with self.subTest(value=value):
                    for operation in (lambda: validate_value("R", value),
                                      lambda: _render(source, {"R": value}),
                                      lambda: create_run(root / "project.json", 0, {"R": value}),
                                      lambda: _validate_points(manifest, {"points": [{"parameters": {"R": value}}]}, cfg)):
                        with self.assertRaises(ValueError):
                            operation()
            self.assertFalse((root / "runs").exists())

    def test_schema(self):
        schema = {"type": "number", "min": "1k", "max": "2k", "log": True}
        self.assertEqual(validate_value("R", "1.5k", schema), "1.5k")
        self.assertEqual(number_value("1.5k"), 1500)
        for value in ("0", "3k", "nan", "inf", "1e999", "R+1"):
            with self.assertRaises(ValueError):
                validate_value("R", value, schema)
        self.assertEqual(validate_value("mode", "fast", {"type": "enum", "candidates": ["fast", "slow"]}), "fast")
        with self.assertRaises(ValueError):
            validate_value("mode", "other", {"type": "enum", "candidates": ["fast"]})
        with self.assertRaises(ValueError):
            validate_values({"unknown": "1"}, {"R": {}})
