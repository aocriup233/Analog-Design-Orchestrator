from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from analog_agent.campaign import add_cycle, brief, create_campaign, propose
from analog_agent.core import create_run, load_config, read_json, write_json
from analog_agent.memory import retrieve, review, submit_candidate


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
        (self.root / "evidence.json").write_text('{"measured": true}', encoding="utf-8")
        private = self.root / "private"
        private.mkdir()
        write_json(private / "memory.json", {"schema": 1, "entries": []})
        (self.root / "proposer.py").write_text(
            "def propose(context):\n"
            "    ids = [item['id'] for item in context['memory']['entries']]\n"
            "    return {'points': [{'id': 'p1', 'parameters': {'R': '1k'}, "
            "'rationale': ','.join(ids)}]}\n", encoding="utf-8")
        self.config = self.root / "project.json"
        write_json(self.config, {
            "template": "template.scs", "parameters": {"R": {"initial": "1k"}},
            "simulation": {"backend": "external"},
            "metrics": {"m": {"kind": "scalar", "key": "m"}}, "rules": {"m": {"min": 0}},
            "memory": {"path": "private/memory.json",
                       "context": {"pdk": "process_a", "topology": "amp", "analysis": "ac"},
                       "max_items": 2, "max_chars": 250},
            "campaign": {"max_parallel": 1, "max_points_per_cycle": 2,
                         "proposer": "proposer.py:propose"},
        })

    def tearDown(self):
        self.temp.cleanup()

    def _candidate(self, identifier, pdk, summary="A measured observation"):
        path = self.root / "private" / f"{identifier}.json"
        write_json(path, {"id": identifier, "kind": "design", "summary": summary,
                          "scope": {"pdk": pdk, "topology": "amp", "analysis": "ac"},
                          "evidence": ["evidence.json"]})
        return path

    def test_reviewed_only_hard_filter_and_bounded_retrieval(self):
        cfg = load_config(self.config)
        submit_candidate(cfg, self._candidate("matching", "process_a"))
        self.assertEqual(retrieve(cfg)["entries"], [])
        review(cfg, "matching", approve=True, rationale="Checked against the evidence")
        submit_candidate(cfg, self._candidate("other_pdk", "process_b"))
        review(cfg, "other_pdk", approve=True, rationale="Valid in its own process")
        submit_candidate(cfg, self._candidate("pending", "process_a"))
        found = retrieve(cfg)
        self.assertEqual([entry["id"] for entry in found["entries"]], ["matching"])
        self.assertLessEqual(found["selected_chars"], 250)
        self.assertNotIn("evidence", found["entries"][0])

    def test_campaign_and_run_snapshot_without_simulation(self):
        cfg = load_config(self.config)
        submit_candidate(cfg, self._candidate("first", "process_a"))
        review(cfg, "first", approve=True, rationale="Measured and reviewed")
        campaign = create_campaign(self.config)
        self.assertEqual(brief(campaign)["memory"]["entries"][0]["id"], "first")
        proposal = propose(campaign)
        self.assertEqual(read_json(Path(proposal["proposal"]))["points"][0]["rationale"], "first")
        self.assertEqual(read_json(Path(proposal["proposal"]).with_name(
            Path(proposal["proposal"]).stem + "_memory.json"))["selection_sha256"],
            proposal["memory_selection_sha256"])
        add_cycle(campaign, Path(proposal["proposal"]))
        self.assertEqual(brief(campaign)["memory"]["entries"][0]["id"], "first")
        run = create_run(self.config, 0)
        self.assertEqual(read_json(run / "memory_snapshot.json")["entries"][0]["id"], "first")
        self.assertFalse((run / "simulation_result.json").exists())

    def test_changed_evidence_cannot_be_approved(self):
        cfg = load_config(self.config)
        submit_candidate(cfg, self._candidate("unstable", "process_a"))
        (self.root / "evidence.json").write_text('{"measured": false}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            review(cfg, "unstable", approve=True, rationale="Review")


if __name__ == "__main__":
    unittest.main()
