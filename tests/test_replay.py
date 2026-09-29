from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analog_agent.core import read_json, write_json
from analog_agent.replay import replay


class OfflineReplayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
        (self.root / "proposer.py").write_text(
            "def propose(context):\n"
            "    selected = [row for row in context['knowledge'].get('observations', []) "
            "if row['selected']]\n"
            "    value = int(selected[-1]['parameters']['R'][:-1]) + 1 if selected else 1\n"
            "    return {'points': [{'id': 'candidate', 'parameters': {'R': f'{value}k'}}]}\n",
            encoding="utf-8")
        self.config = self.root / "project.json"
        write_json(self.config, {
            "template": "template.scs", "parameters": {"R": {"initial": "1k"}},
            "simulation": {"backend": "external"},
            "metrics": {"gain": {"kind": "scalar", "key": "gain"}},
            "rules": {"gain": {"min": 1}},
            "campaign": {"max_parallel": 1, "max_points_per_cycle": 2,
                         "max_cycles": 3, "max_total_points": 4,
                         "proposer": "proposer.py:propose"},
        })
        cycles = []
        for number in (1, 2):
            cycles.append({"source": "proposal", "comparison": {
                "campaign_id": "offline", "cycle": f"cycle-{number:04d}",
                "points": [{"id": f"p{number}", "parameters": {"R": f"{number}k"},
                            "state": "ANALYZED", "verdict": "PASS",
                            "metrics": {"gain": float(number)}}]},
                "decision": {"selected": [f"p{number}"],
                             "rationale": f"review measured point {number}"}})
        self.history = self.root / "history.json"
        write_json(self.history, {"schema": 1, "initial_knowledge": {}, "cycles": cycles})

    def tearDown(self):
        self.temp.cleanup()

    def test_two_cycle_replay_is_deterministic_and_does_not_create_runs(self):
        first = replay(self.config, self.history)
        second = replay(self.config, self.history)
        self.assertEqual(first, second)
        self.assertEqual(first["cycle_count"], 2)
        self.assertEqual(len(first["final_knowledge"]["observations"]), 2)
        self.assertEqual([row["proposed_point_ids"] for row in first["checkpoints"]],
                         [["candidate"], ["candidate"]])
        self.assertFalse((self.root / "runs").exists())
        self.assertFalse((self.root / "campaigns").exists())
        output = subprocess.run([sys.executable, "-m", "analog_agent.cli", "campaign",
                                 "replay", str(self.config), str(self.history)],
                                check=True, capture_output=True, text=True)
        self.assertNotIn("final_knowledge", json.loads(output.stdout))

    def test_replay_rejects_unreviewed_or_unproposed_data(self):
        history = json.loads(self.history.read_text(encoding="utf-8"))
        history["cycles"][1]["comparison"]["points"][0]["parameters"]["R"] = "9k"
        write_json(self.history, history)
        with self.assertRaisesRegex(ValueError, "outside its proposal"):
            replay(self.config, self.history)
        history["cycles"][1]["source"] = "manual"
        history["cycles"][1]["decision"]["selected"] = ["missing"]
        write_json(self.history, history)
        with self.assertRaisesRegex(ValueError, "invalid recorded decision"):
            replay(self.config, self.history)

    def test_manual_history_needs_no_circuit_strategy(self):
        config = read_json(self.config)
        del config["campaign"]["proposer"]
        write_json(self.config, config)
        history = read_json(self.history)
        history.pop("initial_knowledge")
        for cycle in history["cycles"]:
            cycle["source"] = "manual"
        write_json(self.history, history)
        report = replay(self.config, self.history)
        self.assertEqual(report["cycle_count"], 2)
        self.assertEqual(report["checkpoints"][0]["proposed_point_ids"], [])
        self.assertEqual(len(report["final_knowledge"]["observations"]), 2)


if __name__ == "__main__":
    unittest.main()
