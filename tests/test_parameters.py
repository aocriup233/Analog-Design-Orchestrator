import tempfile
import unittest
from pathlib import Path

from analog_agent.parameters import validate_value, validate_values, number_value
from analog_agent.netlist_agent import _render
from analog_agent.campaign import _validate_points
from analog_agent.core import create_run, write_json


class ParameterTest(unittest.TestCase):
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
