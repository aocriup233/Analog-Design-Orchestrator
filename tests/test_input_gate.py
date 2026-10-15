"""Tamper tests for the single read-only stage gate."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent.core import create_run, read_json, write_json
from analog_agent.input_gate import InputMismatch, verify_inputs
from analog_agent.netlist_agent import prepare
from analog_agent.simulation_agent import stage, submit, retrieve


class InputGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("R0 (a b) resistor r=@@R@@\n")
        write_json(self.root / "rule.json", {"kind": "ac", "name": "ac", "options": {"start": "1", "stop": "10", "lin": 3}})
        (self.root / "plugin.py").write_text("def metric(*args): return 1\n")
        write_json(self.root / "private/global/config/overrides.json", {})
        self.config = self.root / "project.json"
        write_json(self.config, {"template": "template.scs", "parameters": {"R": {"initial": "1k"}},
                                 "simulation": {"backend": "external", "rules": ["rule.json"]},
                                 "metrics": {"v": {"kind": "python", "function": "plugin.py:metric"}},
                                 "rules": {"v": {"min": 0}}})

    def test_each_creation_input_tamper_leaves_state_and_files(self):
        for filename in ("project.json", "config.json", "private_manifest.json", "template.scs", "rule.json", "plugin.py",
                         "private/global/config/overrides.json"):
            with self.subTest(filename=filename):
                run = create_run(self.config, 0)
                target = (run if filename in {"config.json", "private_manifest.json"} else self.root) / filename
                original = target.read_bytes()
                before = (run / "task.json").read_bytes()
                target.write_bytes(original + b" ")
                try:
                    with self.assertRaisesRegex(InputMismatch, "expected=.*actual="):
                        prepare(run)
                    self.assertEqual((run / "task.json").read_bytes(), before)
                    self.assertFalse((run / "netlist").exists())
                finally:
                    target.write_bytes(original)

    def test_receipt_and_netlist_tamper(self):
        for receipt in (True, False):
            run = create_run(self.config, 0)
            net = prepare(run)
            target = run / "netlist_result.json" if receipt else Path(net["netlist"])
            target.write_bytes(target.read_bytes() + b" ")
            before = (run / "task.json").read_bytes()
            with self.assertRaises(InputMismatch), patch("analog_agent.simulation_agent.backend_for") as backend:
                stage(run)
            backend.assert_not_called()
            self.assertEqual((run / "task.json").read_bytes(), before)

    def test_gate_does_not_rebaseline(self):
        run = create_run(self.config, 0)
        inventory = read_json(run / "task.json")["pinned_inputs"]
        verify_inputs("netlist", run)
        prepare(run)
        self.assertEqual(read_json(run / "task.json")["pinned_inputs"][:len(inventory)], inventory)

    def test_legacy_inventory_refused(self):
        run = create_run(self.config, 0)
        task = read_json(run / "task.json")
        del task["pinned_inputs"]
        write_json(run / "task.json", task)
        with self.assertRaisesRegex(InputMismatch, "Missing pinned"):
            verify_inputs("netlist", run)

    def test_added_private_override_refused(self):
        run = create_run(self.config, 0)
        write_json(self.root / "private/agents/simulation/config/overrides.json", {})
        with self.assertRaises(InputMismatch):
            verify_inputs("netlist", run)

    def test_deck_and_plan_tamper_prevent_submit(self):
        for artifact in ("deck", "simulation_plan.json"):
            with self.subTest(artifact=artifact):
                run = create_run(self.config, 0)
                prepare(run)
                stage(run)
                plan = read_json(run / "simulation_plan.json")
                target = Path(plan["deck"]) if artifact == "deck" else run / artifact
                target.write_bytes(target.read_bytes() + b" ")
                before = (run / "task.json").read_bytes()
                with self.assertRaises(InputMismatch), patch("analog_agent.simulation_agent.backend_for") as backend:
                    submit(run)
                backend.assert_not_called()
                self.assertEqual((run / "task.json").read_bytes(), before)
                self.assertFalse((run / "submission_intent.json").exists())

    def test_missing_receipt_is_not_an_ordinary_stage_exception(self):
        run = create_run(self.config, 0)
        prepare(run)
        (run / "netlist_result.json").unlink()
        with self.assertRaisesRegex(InputMismatch, "actual=MISSING"):
            verify_inputs("reconcile", run)

    def test_canvas_runtime_modules_are_pinned_without_execution(self):
        runtime = self.root / "runtime"
        for module in ("packages/project-protocol/dist/index.js", "packages/netlist/dist/index.js"):
            path = runtime / module
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// fake compiled runtime\n")
        write_json(self.root / "private/canvas.json", {"canvas_root": str(runtime)})
        cfg = read_json(self.config)
        del cfg["template"]
        cfg["netlist"] = {"block": {"kind": "canvas", "name": "dut", "pins": ["a", "b"],
                                    "project": "template.scs", "canvas_root_config": "private/canvas.json"},
                          "testbenches": {"ac": {"path": "template.scs"}}, "default_testbench": "ac"}
        write_json(self.config, cfg)
        run = create_run(self.config, 0)
        verify_inputs("netlist", run)
        (runtime / "packages/netlist/dist/index.js").write_text("// changed runtime\n")
        before = (run / "task.json").read_bytes()
        with self.assertRaisesRegex(InputMismatch, "expected=.*actual="), patch("analog_agent.netlist_agent.subprocess.run") as exporter:
            prepare(run)
        exporter.assert_not_called()
        self.assertEqual((run / "task.json").read_bytes(), before)
