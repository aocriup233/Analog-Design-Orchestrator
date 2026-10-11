import tempfile
import unittest
from pathlib import Path

from analog_agent.core import create_run, read_json, sha256, write_json
from analog_agent.layout_runs import layout_context, layout_lock, prepare_layout
from analog_agent.netlist_agent import prepare
from analog_agent.state import TRANSITIONS, transition


class LayoutRunsFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        external = self.base / "external"
        external.mkdir()
        (self.root / "template.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
        self.config = self.root / "project.json"
        write_json(self.config, {"template": "template.scs", "parameters": {},
                   "metrics": {"x": {"kind": "scalar", "key": "x"}}, "rules": {"x": {"min": 0}}})
        self.layout_config = self.root / "private/layout/config.json"
        section = {"rule_dir": str(external), "run_dir": str(external)}
        write_json(self.layout_config, {"layout": {"tool": "calibre", "library": "worklib",
                   "cell": "top", "drc": section, "lvs": section}})
        self.source = create_run(self.config, 0)
        self.net = prepare(self.source)

    def verify_source(self):
        for state in ("STAGED", "SUBMITTED", "DONE", "RETRIEVED", "VERIFIED"):
            transition(self.source, state)
        write_json(self.source / "simulation_result.json", {"status": "DONE", "errors": [],
                   "netlist_sha256": self.net["netlist_sha256"]})


class LayoutRunsTest(LayoutRunsFixture):
    def test_linked_run_is_separate_and_does_not_mutate_schematic(self):
        with self.assertRaisesRegex(ValueError, "VERIFIED"):
            prepare_layout(self.source)
        self.verify_source()
        hashes = {p.name: sha256(p) for p in self.source.iterdir() if p.is_file()}
        run = prepare_layout(self.source)
        self.assertEqual(run.parent, self.root / "layouts")
        self.assertEqual(read_json(run / "task.json")["status"], "LAYOUT_PREPARED")
        self.assertEqual(hashes, {p.name: sha256(p) for p in self.source.iterdir() if p.is_file()})
        layout_context(run)
        transition(self.source, "ANALYZED")
        layout_context(run)

    def test_netlist_and_private_inputs_are_rechecked(self):
        self.verify_source()
        run = prepare_layout(self.source)
        Path(self.net["netlist"]).write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            layout_context(run)
        Path(self.net["netlist"]).write_text("simulator lang=spectre\n", encoding="utf-8")
        cfg = read_json(self.layout_config)
        cfg["layout"]["cell"] = "new_cell"
        write_json(self.layout_config, cfg)
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            layout_context(run)

    def test_strict_layout_states_and_attention(self):
        self.verify_source()
        run = prepare_layout(self.source)
        with self.assertRaises(ValueError):
            transition(run, "ROUTED")
        with self.assertRaises(ValueError):
            transition(self.source, "LAYOUT_PREPARED")
        states = ["LAYOUT_PREPARED", "CONSTRAINTS_DRAFTED", "CONSTRAINTS_FROZEN",
                  "FLOORPLAN_PENDING", "FLOORPLAN_FROZEN", "ROUTED", "DRC_DONE", "LVS_DONE", "LAYOUT_VERIFIED"]
        for state in states:
            self.assertIn("NEEDS_ATTENTION", TRANSITIONS[state])
        for state in states[1:]:
            transition(run, state)
        transition(run, "FLOORPLAN_PENDING")
        transition(run, "NEEDS_ATTENTION")
        with self.assertRaises(ValueError):
            transition(run, "FLOORPLAN_FROZEN")

    def test_input_handoff_and_lock(self):
        self.verify_source()
        run = prepare_layout(self.source)
        with layout_lock(run):
            with self.assertRaisesRegex(ValueError, "locked"):
                with layout_lock(run):
                    pass
        self.assertFalse((run / ".layout.lock").exists())
        write_json(run / "layout_inputs.json", {})
        with self.assertRaisesRegex(ValueError, "handoff changed"):
            layout_context(run)

    def test_bad_simulation_receipt_rejected(self):
        self.verify_source()
        write_json(self.source / "simulation_result.json", {"status": "FAILED", "errors": [],
                   "netlist_sha256": self.net["netlist_sha256"]})
        with self.assertRaisesRegex(ValueError, "simulation handoff"):
            prepare_layout(self.source)
