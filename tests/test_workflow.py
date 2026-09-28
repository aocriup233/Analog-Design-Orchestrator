from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from analog_agent.analysis_agent import analyze
from analog_agent.core import create_run, read_json, write_json
from analog_agent.netlist_agent import prepare
from analog_agent.simulation_agent import simulate
from analog_agent.workflow import cleanup
from analog_agent.state import transition


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("simulator lang=spectre\nR0 (A B) resistor r=@@R@@\n", encoding="utf-8")
        self.config = self.root / "project.json"
        write_json(self.config, {
            "template": "template.scs",
            "parameters": {"R": {"initial": "1k", "candidates": ["1k", "2k"]}},
            "simulation": {"backend": "bridge", "output_format": "psfascii"},
            "metrics": {
                "dc": {"kind": "scalar", "key": "dc_A"},
                "gain": {"kind": "ac_db_at", "signal": "ac_A", "at_hz": 1e9},
            },
            "rules": {"dc": {"min": 0.9}, "gain": {"min": -1.0}},
            "workflow": {"max_iterations": 2, "cleanup": "after_success"},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_three_role_handoff_and_cleanup(self):
        run = create_run(self.config, 1)
        net = prepare(run)
        self.assertIn("r=2k", Path(net["netlist"]).read_text(encoding="utf-8"))

        class FakeSimulator:
            def run_simulation(self, path, params):
                self_path = Path(path)
                assert self_path.is_file()
                assert params["include_files"] == []
                return SimpleNamespace(ok=True, status=SimpleNamespace(value="success"),
                    errors=[], warnings=[], metadata={"output_dir": str(run / "raw")},
                    data={"dc_A": 1.0, "ac_freq": [1e8, 1e9],
                          "ac_A": [1 + 0j, 0.95 + 0j]})

        sim = simulate(run, simulator_factory=FakeSimulator)
        self.assertEqual(sim["status"], "DONE")
        report = analyze(run)
        self.assertEqual(report["status"], "PASS")
        raw = run / "raw"
        raw.mkdir()
        (raw / "dummy").write_text("temporary", encoding="utf-8")
        self.assertTrue(cleanup(run))
        self.assertFalse(raw.exists())
        self.assertTrue((run / "analysis_result.json").is_file())
        self.assertEqual(read_json(run / "task.json")["status"], "ANALYZED")

    def test_modified_netlist_rejected(self):
        run = create_run(self.config, 0)
        net = prepare(run)
        Path(net["netlist"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            simulate(run, simulator_factory=lambda: None)

    def test_failed_bridge_does_not_reach_verified(self):
        run = create_run(self.config, 0)
        prepare(run)

        class FailedSimulator:
            def run_simulation(self, path, params):
                return SimpleNamespace(ok=False, status=SimpleNamespace(value="failure"),
                                       errors=["simulator error"], warnings=[], metadata={}, data={})

        result = simulate(run, simulator_factory=FailedSimulator)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(read_json(run / "simulation_result.json")["errors"], ["simulator error"])
        self.assertEqual(read_json(run / "task.json")["status"], "FAILED")

    def test_analysis_requires_completed_simulation(self):
        run = create_run(self.config, 0)
        prepare(run)
        write_json(run / "simulation_result.json", {"status": "FAILED", "errors": ["bad netlist"]})
        with self.assertRaisesRegex(ValueError, "not completed"):
            analyze(run)

    def test_project_local_metric_plugin(self):
        (self.root / "custom_metric.py").write_text(
            "def evaluate(data, definition, run):\n"
            "    return data[definition['key']] * 2\n", encoding="utf-8")
        cfg = read_json(self.config)
        cfg["metrics"] = {"double_dc": {"kind": "python", "function":
                          "custom_metric.py:evaluate", "key": "dc_A"}}
        cfg["rules"] = {"double_dc": {"min": 1.5}}
        write_json(self.config, cfg)
        run = create_run(self.config, 0)
        net = prepare(run)
        write_json(run / "simulation_result.json", {
            "status": "DONE", "netlist_sha256": net["netlist_sha256"],
            "data": {"dc_A": 1.0},
        })
        for state in ("STAGED", "SUBMITTED", "DONE", "RETRIEVED", "VERIFIED"):
            transition(run, state)
        self.assertEqual(analyze(run)["metrics"]["double_dc"], 2.0)

    def test_project_local_netlist_generator(self):
        (self.root / "generate.py").write_text(
            "def build(config, task, run):\n"
            "    path = run / 'generated.scs'\n"
            "    path.write_text('simulator lang=spectre\\n', encoding='utf-8')\n"
            "    return path\n", encoding="utf-8")
        cfg = read_json(self.config)
        del cfg["template"]
        cfg["netlist"] = {"generator": "generate.py:build"}
        write_json(self.config, cfg)
        run = create_run(self.config, 0)
        net = prepare(run)
        self.assertEqual(Path(net["netlist"]).read_text(encoding="utf-8"),
                         "simulator lang=spectre\n")


if __name__ == "__main__":
    unittest.main()
