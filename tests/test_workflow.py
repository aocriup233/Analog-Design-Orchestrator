from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from analog_agent.analysis_agent import analyze
from analog_agent.core import create_run, read_json, write_json
from analog_agent.netlist_agent import prepare
from analog_agent.simulation_agent import simulate, stage
from analog_agent.workflow import cleanup, decide_run, role_obligation
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

    def test_simulation_role_remains_responsible_after_external_staging(self):
        cfg = read_json(self.config)
        cfg["simulation"]["backend"] = "external"
        write_json(self.config, cfg)
        run = create_run(self.config, 0)
        self.assertEqual(role_obligation(run)["owner_role"], "netlist")
        prepare(run)
        self.assertEqual(role_obligation(run)["owner_role"], "simulation")
        stage(run)
        duty = role_obligation(run)
        self.assertEqual(duty["status"], "STAGED")
        self.assertEqual(duty["owner_role"], "simulation")
        self.assertEqual(duty["next_action"], "reconcile_before_submit")
        self.assertFalse(duty["role_complete"])
        transition(run, "SUBMITTED")
        transition(run, "RUN")
        transition(run, "DONE")
        transition(run, "RETRIEVED")
        self.assertEqual(role_obligation(run)["owner_role"], "simulation")
        transition(run, "VERIFIED")
        self.assertEqual(role_obligation(run)["owner_role"], "analysis")

    def test_failed_run_routes_through_analysis_to_next_owner(self):
        cfg = read_json(self.config)
        cfg["simulation"]["backend"] = "external"
        write_json(self.config, cfg)
        run = create_run(self.config, 0)
        prepare(run)
        stage(run)
        transition(run, "FAILED")
        self.assertEqual(role_obligation(run)["owner_role"], "analysis")
        self.assertEqual(role_obligation(run)["next_action"], "triage_failure")
        with self.assertRaisesRegex(ValueError, "reason"):
            decide_run(run, "iterate", "", "netlist")
        routed = decide_run(run, "iterate", "PDK include closure incomplete", "netlist")
        self.assertEqual(routed["owner_role"], "netlist")
        self.assertEqual(routed["next_action"], "create_successor_run")
        with self.assertRaises(FileExistsError):
            decide_run(run, "iterate", "second decision", "simulation")

    def test_verified_run_requires_analysis_decision_before_closure(self):
        run = create_run(self.config, 0)
        prepare(run)
        for state in ("STAGED", "SUBMITTED", "DONE", "RETRIEVED", "VERIFIED", "ANALYZED"):
            transition(run, state)
        self.assertEqual(role_obligation(run)["owner_role"], "analysis")
        closed = decide_run(run, "accept", "Reviewed electrical result")
        self.assertTrue(closed["role_complete"])
        self.assertIsNone(closed["owner_role"])

    def test_remote_cleanup_gates_decision_handoff_when_required(self):
        cfg = read_json(self.config)
        cfg["simulation"]["remote_cleanup_required"] = True
        write_json(self.config, cfg)
        run = create_run(self.config, 0)
        prepare(run)
        for state in ("STAGED", "SUBMITTED", "DONE", "RETRIEVED", "VERIFIED", "ANALYZED"):
            transition(run, state)
        pending = decide_run(run, "accept", "Reviewed local result")
        self.assertEqual(pending["owner_role"], "simulation")
        self.assertEqual(pending["next_action"], "cleanup_remote")
        self.assertFalse(pending["role_complete"])
        write_json(run / "remote_cleanup.json", {"status": "DONE", "backend_data": {
            "verified_absent": ["/site/run"]}})
        completed = role_obligation(run)
        self.assertTrue(completed["role_complete"])
        self.assertIsNone(completed["owner_role"])

    def test_external_cleanup_receipt_requires_local_archive_and_absence(self):
        cfg = read_json(self.config)
        cfg["simulation"]["backend"] = "external"
        cfg["simulation"]["remote_cleanup_required"] = True
        write_json(self.config, cfg)
        run = create_run(self.config, 0)
        prepare(run)
        for state in ("STAGED", "SUBMITTED", "DONE", "RETRIEVED", "VERIFIED"):
            transition(run, state)
        decide_run(run, "iterate", "Bias revision", "netlist")
        archive = run / "result.tar"
        archive.write_bytes(b"local verified result")
        from analog_agent.core import sha256
        receipt = {"status": "VERIFIED_CLEANED", "run_id": run.name,
                   "local_result_archive": str(archive), "local_result_sha256": sha256(archive),
                   "paths": [{"path": "/site/run", "verified_absent": False,
                              "observed_stdout": ""}]}
        write_json(run / "remote_cleanup.json", receipt)
        self.assertEqual(role_obligation(run)["next_action"], "cleanup_remote")
        receipt["paths"][0].update(verified_absent=True, observed_stdout="absent: run")
        write_json(run / "remote_cleanup.json", receipt)
        self.assertEqual(role_obligation(run)["owner_role"], "netlist")
        archive.write_bytes(b"changed")
        self.assertEqual(role_obligation(run)["next_action"], "cleanup_remote")

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

    def test_one_block_multiple_testbenches(self):
        (self.root / "block.scs").write_text(
            "subckt dut (IN OUT VSS)\nR0 (IN OUT) resistor r=@@R@@\nends dut\n",
            encoding="utf-8")
        (self.root / "dc_tb.scs").write_text(
            "VIN (IN 0) vsource dc=0.1\nX0 (IN OUT 0) dut\n", encoding="utf-8")
        (self.root / "ac_tb.scs").write_text(
            "VIN (IN 0) vsource dc=0 acmag=1\nX0 (IN OUT 0) dut\n", encoding="utf-8")
        cfg = read_json(self.config)
        del cfg["template"]
        cfg["netlist"] = {
            "block": {"kind": "source", "path": "block.scs", "name": "dut",
                      "pins": ["IN", "OUT", "VSS"]},
            "testbenches": {"dc": {"path": "dc_tb.scs"},
                            "ac": {"path": "ac_tb.scs"}},
            "default_testbench": "dc",
        }
        write_json(self.root / "dc_rule.json", {"kind": "dc", "name": "dcOp", "options": {}})
        write_json(self.root / "ac_rule.json", {"kind": "ac", "name": "acSweep",
                                                  "options": {"start": "1", "stop": "1G", "dec": 10}})
        cfg["simulation"]["rules_by_testbench"] = {
            "dc": ["dc_rule.json"], "ac": ["ac_rule.json"]}
        write_json(self.config, cfg)
        dc = prepare(create_run(self.config, 0))
        ac = prepare(create_run(self.config, 0, testbench="ac"))
        self.assertEqual(dc["block"]["sha256"], ac["block"]["sha256"])
        self.assertNotEqual(dc["testbench"]["sha256"], ac["testbench"]["sha256"])
        self.assertIn("acmag=1", Path(ac["netlist"]).read_text(encoding="utf-8"))
        self.assertEqual(ac["testbench"]["name"], "ac")
        ac_run = Path(ac["netlist"]).parent.parent
        stage(ac_run)
        plan = read_json(ac_run / "simulation_plan.json")
        self.assertEqual(plan["testbench"], "ac")
        self.assertIn("acSweep ac", plan["statements"][0])
        with self.assertRaisesRegex(ValueError, "Unknown testbench"):
            create_run(self.config, 0, testbench="missing")

    def test_split_block_model_bundle_uses_current_private_config(self):
        (self.root / "block.scs").write_text(
            "// BEGIN MODELS\ninclude \"stale.scs\" section=OLD\n// END MODELS\n"
            "subckt dut (IN OUT VSS)\nR0 (IN OUT) resistor r=@@R@@\nends dut\n",
            encoding="utf-8")
        (self.root / "tb.scs").write_text("X0 (IN OUT 0) dut\n", encoding="utf-8")
        write_json(self.root / "models.json", {"section": "NOM"})
        (self.root / "render.py").write_text(
            "def render(config):\n"
            "    return '// BEGIN MODELS\\ninclude \\\"model.scs\\\" section=' "
            "+config['section']+'\\n// END MODELS'\n", encoding="utf-8")
        cfg = read_json(self.config)
        del cfg["template"]
        cfg["netlist"] = {
            "block": {"kind": "source", "path": "block.scs", "name": "dut",
                      "pins": ["IN", "OUT", "VSS"],
                      "model_bundle": {"config": "models.json",
                                       "renderer": "render.py:render",
                                       "begin": "// BEGIN MODELS", "end": "// END MODELS"}},
            "testbenches": {"dc": {"path": "tb.scs"}}, "default_testbench": "dc"}
        write_json(self.config, cfg)
        first = prepare(create_run(self.config, 0))
        self.assertIn("section=NOM", Path(first["netlist"]).read_text(encoding="utf-8"))
        self.assertIn("models.json", first["block"]["dependencies"])
        write_json(self.root / "models.json", {"section": "FAST"})
        second = prepare(create_run(self.config, 0))
        self.assertIn("section=FAST", Path(second["netlist"]).read_text(encoding="utf-8"))
        self.assertNotEqual(first["block"]["sha256"], second["block"]["sha256"])

    def test_canvas_adapter_requires_explicit_port_contract(self):
        (self.root / "drawing.icproj.json").write_text("{}", encoding="utf-8")
        (self.root / "mapping.py").write_text(
            "def export(config, task, run, values):\n"
            "    path = run / 'mapped.scs'\n"
            "    path.write_text('subckt dut (A B)\\nR0 (A B) resistor r=1k\\nends dut\\n', encoding='utf-8')\n"
            "    return path\n", encoding="utf-8")
        (self.root / "tb.scs").write_text("X0 (A B) dut\n", encoding="utf-8")
        cfg = read_json(self.config)
        del cfg["template"]
        cfg["netlist"] = {
            "block": {"kind": "canvas", "project": "drawing.icproj.json",
                      "adapter": "mapping.py:export", "name": "dut", "pins": ["A", "B"]},
            "testbenches": {"smoke": {"path": "tb.scs"}},
            "default_testbench": "smoke",
        }
        write_json(self.config, cfg)
        prepared = prepare(create_run(self.config, 0))
        self.assertEqual(prepared["block"]["kind"], "canvas")
        cfg["netlist"]["block"]["pins"] = ["B", "A"]
        write_json(self.config, cfg)
        with self.assertRaisesRegex(ValueError, "port order"):
            prepare(create_run(self.config, 0))

    def test_canvas_runtime_location_comes_from_project_private_config(self):
        canvas_root = self.root / "canvas_checkout"
        protocol = canvas_root / "packages" / "project-protocol" / "dist"
        netlist = canvas_root / "packages" / "netlist" / "dist"
        protocol.mkdir(parents=True)
        netlist.mkdir(parents=True)
        (canvas_root / "package.json").write_text('{"type":"module"}', encoding="utf-8")
        (protocol / "index.js").write_text(
            "export const parseProject = JSON.parse;\n", encoding="utf-8")
        (netlist / "index.js").write_text(
            "export const unfinishedDrawingDiagnostics = () => [];\n"
            "export const createDesignNetlistExport = () => ({status:'ready',"
            " diagnostics:[], file:{text:'simulator lang=spectre\\nsubckt dut (A B)\\n"
            "R0 (A B) resistor r=1k\\nends dut\\n'}});\n", encoding="utf-8")
        (self.root / "drawing.icproj.json").write_text("{}", encoding="utf-8")
        (self.root / "tb.scs").write_text("X0 (A B) dut\n", encoding="utf-8")
        write_json(self.root / "private" / "canvas.json", {"canvas_root": str(canvas_root)})
        cfg = read_json(self.config)
        del cfg["template"]
        cfg["netlist"] = {
            "block": {"kind": "canvas", "project": "drawing.icproj.json",
                      "canvas_root_config": "private/canvas.json", "name": "dut",
                      "pins": ["A", "B"]},
            "testbenches": {"dc": {"path": "tb.scs"}}, "default_testbench": "dc"}
        write_json(self.config, cfg)
        prepared = prepare(create_run(self.config, 0))
        self.assertIn("R0 (A B) resistor r=1k", Path(prepared["netlist"]).read_text(encoding="utf-8"))
        self.assertIn("private/canvas.json", prepared["block"]["dependencies"])
        self.assertIn("canvas_runtime/packages/netlist/dist/index.js", prepared["block"]["dependencies"])


if __name__ == "__main__":
    unittest.main()
