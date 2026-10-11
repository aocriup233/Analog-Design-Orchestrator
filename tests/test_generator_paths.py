import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent.core import create_run, write_json
from analog_agent.netlist_agent import prepare, _split_prepare


class GeneratorPathTest(unittest.TestCase):
    def test_all_generator_returns_confined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.scs"
            outside.write_text("subckt cell (a b)\nends cell\n")
            cfg = {"_project_root": str(root), "parameters": {},
                   "netlist": {"generator": "generator.py:run"},
                   "metrics": {"v": {}}, "rules": {"v": {}}}
            write_json(root / "project.json", cfg)
            run = create_run(root / "project.json", 0)
            with patch("analog_agent.netlist_agent.load_function", return_value=lambda *args: outside):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    prepare(run)
            cfg["netlist"] = {"default_testbench": "test", "block": {
                "kind": "canvas", "name": "cell", "pins": ["a", "b"],
                "project": "outside.scs", "adapter": "fake:run"},
                "testbenches": {"test": {"generator": "fake:run"}}}
            with patch("analog_agent.netlist_agent.load_function", return_value=lambda *args: outside):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    _split_prepare(cfg, {}, run, {})
                cfg["netlist"]["block"] = {"kind": "source", "path": "outside.scs",
                                           "name": "cell", "pins": ["a", "b"]}
                with self.assertRaisesRegex(ValueError, "escapes"):
                    _split_prepare(cfg, {}, run, {})
            valid = run / "generated.scs"
            valid.write_text("// generated")
            cfg["netlist"] = {"generator": "generator.py:run"}
            with patch("analog_agent.netlist_agent.load_function", return_value=lambda *args: "generated.scs"):
                self.assertEqual(prepare(run)["source"], str(valid))
