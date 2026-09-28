from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from analog_agent.core import create_run, read_json, run_config, write_json
from analog_agent.netlist_agent import prepare
from analog_agent.sim_rules import build_deck, render_rule
from analog_agent.simulation_agent import simulate


class SimulationRulesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "design.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
        write_json(self.root / "dc.json", {"kind": "dc", "name": "dcOp", "options": {}})
        write_json(self.root / "ac.json", {"kind": "ac", "name": "ac", "options":
                   {"start": "1M", "stop": "10G", "dec": 20}})
        write_json(self.root / "tran.json", {"kind": "tran", "name": "tran", "options":
                   {"stop": "10n", "maxstep": "1p"}})
        self.project = self.root / "project.json"
        write_json(self.project, {
            "template": "design.scs", "parameters": {},
            "simulation": {"rules": ["dc.json", "ac.json", "tran.json"],
                           "save_signals": ["VIN", "VOUT"], "backend": "bridge"},
            "metrics": {"v": {"kind": "scalar", "key": "dc_VOUT"}},
            "rules": {"v": {"min": 0}},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_deck_is_separate_from_design(self):
        run = create_run(self.project, 0)
        design = Path(prepare(run)["netlist"])
        plan = build_deck(design, run_config(run), run)
        deck = Path(plan["deck"]).read_text(encoding="utf-8")
        self.assertIn("dcOp dc", deck)
        self.assertIn("ac ac start=1M stop=10G dec=20", deck)
        self.assertIn("tran tran stop=10n maxstep=1p", deck)
        self.assertIn("save VIN VOUT", deck)
        self.assertNotIn("ac ac", design.read_text(encoding="utf-8"))

    def test_invalid_rule_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires start and stop"):
            render_rule({"kind": "ac", "options": {"dec": 10}})
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            render_rule({"kind": "tran", "options": {"stop": "1n\ninclude bad"}})

    def test_simulation_worker_uses_generated_deck(self):
        run = create_run(self.project, 0)
        design = Path(prepare(run)["netlist"])

        class FakeSimulator:
            def run_simulation(self, netlist, params):
                assert Path(netlist).name == "simulation.scs"
                assert str(design) in params["include_files"]
                return SimpleNamespace(ok=True, status=SimpleNamespace(value="success"),
                                       errors=[], warnings=[], metadata={}, data={"dc_VOUT": 1.0})

        result = simulate(run, simulator_factory=FakeSimulator)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["simulation_plan_sha256"],
                         read_json(run / "simulation_plan.json")["deck_sha256"])

    def test_private_override_is_hashed_not_snapshotted(self):
        private = self.root / "private" / "global" / "config" / "overrides.json"
        private.parent.mkdir(parents=True)
        write_json(private, {"simulation": {"profile": "site_profile"}})
        run = create_run(self.project, 0)
        self.assertEqual(run_config(run)["simulation"]["profile"], "site_profile")
        self.assertNotIn("site_profile", (run / "config.json").read_text(encoding="utf-8"))
        self.assertIn("private/global/config/overrides.json", read_json(run / "private_manifest.json"))
        write_json(private, {"simulation": {"profile": "different"}})
        with self.assertRaisesRegex(ValueError, "changed"):
            run_config(run)

    def test_custom_analysis_renderer(self):
        (self.root / "extra.py").write_text(
            "def render(rule):\n"
            "    return f\"noise1 noise start={rule['start']} stop={rule['stop']}\"\n",
            encoding="utf-8")
        statement = render_rule({"kind": "python", "function": "extra.py:render",
                                 "start": "1k", "stop": "1G"}, self.root)
        self.assertEqual(statement, "noise1 noise start=1k stop=1G")


if __name__ == "__main__":
    unittest.main()
