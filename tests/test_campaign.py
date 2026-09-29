from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analog_agent.campaign import (add_cycle, attach_verified_run, brief, create_campaign, finish, inspect_campaign,
                                   pause, propose, resume, retry, step)
from analog_agent.core import read_json, write_json
from analog_agent.simulation_agent import reconcile_submission
from analog_agent.state import status as run_status, transition


ADAPTER = '''\
from analog_agent.core import read_json, sha256

class Adapter:
    def stage(self, run, config, plan):
        return {"input_sha256": plan["deck_sha256"]}

    def submit(self, run, config, staged):
        marker = run / "submit_count"
        marker.write_text(str(int(marker.read_text()) + 1 if marker.exists() else 1))
        return {"state": "SUBMITTED", "job_id": run.name}

    def reconcile(self, run, config, staged, intent):
        if not (run / "submit_count").is_file() or intent["run_id"] != run.name:
            raise ValueError("No matching scheduler job")
        return {"state": "SUBMITTED", "job_id": run.name}

    def poll(self, run, config, submitted):
        marker = run / "poll_count"
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        return {"state": "RUN" if count == 1 else "DONE", "job_id": submitted["job_id"]}

    def retrieve(self, run, config, submitted):
        path = run / "raw" / "fake.psf"
        path.parent.mkdir(exist_ok=True)
        path.write_text("fake")
        digest = sha256(path)
        return {"artifacts": [{"path": str(path), "sha256": digest,
                                "remote_sha256": digest}]}

    def verify(self, run, config, submitted, retrieved):
        params = read_json(run / "netlist_result.json")["parameters"]
        return {"ok": True, "data": {"dc_A": float(params["R"].replace("k", ""))}}

    def cleanup(self, run, config, submitted):
        return {"status": "DONE"}

def create(config, run):
    return Adapter()
'''

PROPOSER = '''\
def propose(context):
    if context["cycle_count"] == 0:
        return {"points": [{"id": "initial", "parameters": {"R": "1k"},
                            "rationale": "Private initial-point choice"}]}
    return {"points": [{"id": "followup", "parameters": {"R": "3k"},
                        "rationale": "Private next-point choice"}]}
'''


class CampaignTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text(
            "simulator lang=spectre\nR0 (A B) resistor r=@@R@@\n", encoding="utf-8")
        (self.root / "adapter.py").write_text(ADAPTER, encoding="utf-8")
        (self.root / "proposer.py").write_text(PROPOSER, encoding="utf-8")
        self.config = self.root / "project.json"
        write_json(self.config, {
            "template": "template.scs", "parameters": {"R": {"initial": "1k"}},
            "simulation": {"backend": "lsf", "adapter": "adapter.py:create"},
            "metrics": {"dc": {"kind": "scalar", "key": "dc_A"}},
            "rules": {"dc": {"min": 0.5}},
            "workflow": {"submission": "manual", "cleanup": "never"},
            "campaign": {"max_parallel": 2, "max_points_per_cycle": 3,
                         "max_cycles": 3, "max_total_points": 5,
                         "poll_interval_s": 1, "metric_directions": {"dc": "max"},
                         "proposer": "proposer.py:propose"},
        })

    def tearDown(self):
        self.temp.cleanup()

    def _points(self, values):
        path = self.root / f"points_{len(list(self.root.glob('points_*'))) + 1}.json"
        write_json(path, {"points": [{"id": f"p{index}", "parameters": {"R": value}}
                                     for index, value in enumerate(values, 1)]})
        return path

    def test_same_parameters_can_target_distinct_testbenches(self):
        (self.root / "block.scs").write_text(
            "subckt dut (A B)\nR0 (A B) resistor r=@@R@@\nends dut\n", encoding="utf-8")
        (self.root / "dc_tb.scs").write_text("X0 (A B) dut\n", encoding="utf-8")
        (self.root / "ac_tb.scs").write_text("X0 (A B) dut\n", encoding="utf-8")
        cfg = read_json(self.config)
        del cfg["template"]
        cfg["netlist"] = {"block": {"kind": "source", "path": "block.scs",
                                    "name": "dut", "pins": ["A", "B"]},
                          "testbenches": {"dc": {"path": "dc_tb.scs"},
                                          "ac": {"path": "ac_tb.scs"}},
                          "default_testbench": "dc"}
        write_json(self.config, cfg)
        points = self.root / "tb_points.json"
        write_json(points, {"points": [
            {"id": "dc", "parameters": {"R": "1k"}, "testbench": "dc"},
            {"id": "ac", "parameters": {"R": "1k"}, "testbench": "ac"}]})
        campaign = create_campaign(self.config)
        add_cycle(campaign, points)
        selected = read_json(campaign / "campaign.json")["cycles"][0]["points"]
        self.assertEqual([point["testbench"] for point in selected], ["dc", "ac"])
        self.assertNotEqual(selected[0]["fingerprint"], selected[1]["fingerprint"])

    def test_bounded_parallel_cycle_and_reviewed_next_cycle(self):
        campaign = create_campaign(self.config)
        add_cycle(campaign, self._points(["1k", "2k", "3k"]))
        first = step(campaign)
        self.assertEqual(first["status"], "ACTIVE")
        self.assertEqual(first["cycles"][0]["counts"], {"QUEUED": 1, "SUBMITTED": 2})
        self.assertEqual(step(campaign)["cycles"][0]["counts"], {"QUEUED": 1, "RUN": 2})
        third = step(campaign)
        self.assertEqual(third["cycles"][0]["counts"], {"ANALYZED": 2, "SUBMITTED": 1})
        self.assertEqual(step(campaign)["status"], "ACTIVE")
        reviewed = step(campaign)
        self.assertEqual(reviewed["status"], "REVIEW")
        comparison = read_json(campaign / "cycle-0001_comparison.json")
        self.assertEqual(comparison["pareto_point_ids"], ["p3"])
        self.assertEqual(len(comparison["points"]), 3)
        packet = brief(campaign)
        self.assertEqual(packet["latest_comparison"]["pareto_point_ids"], ["p3"])
        self.assertTrue(all("data" not in row for row in packet["latest_comparison"]["points"]))
        for point in read_json(campaign / "campaign.json")["cycles"][0]["points"]:
            self.assertEqual((Path(point["run"]) / "submit_count").read_text(), "1")
            self.assertEqual(run_status(Path(point["run"])), "ANALYZED")
        with self.assertRaisesRegex(ValueError, "Duplicate parameter point"):
            add_cycle(campaign, self._points(["2k", "2k"]), decision="Try another point")
        add_cycle(campaign, self._points(["4k"]), decision="Continue the tradeoff search",
                  selected=["p3"])
        self.assertEqual(read_json(campaign / "campaign.json")["cycles"][0]
                         ["decision"]["selected"], ["p3"])
        for _ in range(3):
            step(campaign)
        self.assertEqual(inspect_campaign(campaign)["status"], "REVIEW")
        self.assertEqual(finish(campaign, "Prefer the fourth point", ["p1"])["status"], "CLOSED")

    def test_pause_resume_and_private_proposal(self):
        campaign = create_campaign(self.config)
        suggestion = propose(campaign)
        self.assertTrue(suggestion["review_required"])
        add_cycle(campaign, Path(suggestion["proposal"]))
        step(campaign)
        self.assertEqual(pause(campaign)["status"], "PAUSED")
        self.assertEqual(step(campaign)["status"], "PAUSED")
        self.assertEqual(resume(campaign)["status"], "ACTIVE")
        for _ in range(2):
            step(campaign)
        self.assertEqual(inspect_campaign(campaign)["status"], "REVIEW")
        self.assertEqual(propose(campaign)["point_count"], 1)

    def test_uncertain_submission_is_not_retried(self):
        campaign = create_campaign(self.config)
        add_cycle(campaign, self._points(["1k"]))
        step(campaign)
        point = read_json(campaign / "campaign.json")["cycles"][0]["points"][0]
        run = Path(point["run"])
        self.assertTrue((run / "submission_intent.json").is_file())
        (run / "submission_result.json").unlink()
        task = read_json(run / "task.json")
        task["status"] = "STAGED"  # interrupted after remote submit, before local handoff
        write_json(run / "task.json", task)
        self.assertIn("Submission outcome unknown", inspect_campaign(campaign)["cycles"][0]
                      ["points"][0]["issue"])
        self.assertEqual(step(campaign)["status"], "NEEDS_ATTENTION")
        with self.assertRaisesRegex(ValueError, "Submission outcome unknown"):
            retry(campaign, "p1")
        self.assertEqual((run / "submit_count").read_text(), "1")
        self.assertEqual(reconcile_submission(run)["status"], "SUBMITTED")
        self.assertEqual(retry(campaign, "p1")["status"], "ACTIVE")
        for _ in range(2):
            step(campaign)
        self.assertEqual(inspect_campaign(campaign)["status"], "REVIEW")
        self.assertEqual((run / "submit_count").read_text(), "1")

    def test_cli_step_recovers_without_process_memory(self):
        campaign = create_campaign(self.config)
        add_cycle(campaign, self._points(["1k", "2k"]))
        for _ in range(3):
            completed = subprocess.run(
                [sys.executable, "-m", "analog_agent.cli", "campaign", "step", str(campaign)],
                capture_output=True, text=True, check=True)
            report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "REVIEW")
        self.assertEqual(len(read_json(campaign / "cycle-0001_comparison.json")["points"]), 2)

    def test_changed_config_blocks_new_submissions_until_restored(self):
        campaign = create_campaign(self.config)
        add_cycle(campaign, self._points(["1k"]))
        original = self.config.read_text(encoding="utf-8")
        self.config.write_text(original + "\n", encoding="utf-8")
        blocked = step(campaign)
        self.assertEqual(blocked["status"], "NEEDS_ATTENTION")
        self.assertIn("changed during this cycle", blocked["cycles"][0]["issue"])
        self.assertEqual(blocked["cycles"][0]["counts"], {"QUEUED": 1})
        self.config.write_text(original, encoding="utf-8")
        self.assertEqual(step(campaign)["cycles"][0]["counts"], {"SUBMITTED": 1})

    def test_changed_template_blocks_same_cycle(self):
        campaign = create_campaign(self.config)
        add_cycle(campaign, self._points(["1k"]))
        template = self.root / "template.scs"
        original = template.read_text(encoding="utf-8")
        template.write_text(original + "// revised\n", encoding="utf-8")
        blocked = step(campaign)
        self.assertEqual(blocked["status"], "NEEDS_ATTENTION")
        self.assertEqual(blocked["cycles"][0]["counts"], {"QUEUED": 1})
        template.write_text(original, encoding="utf-8")
        self.assertEqual(step(campaign)["cycles"][0]["counts"], {"SUBMITTED": 1})

    def test_changed_adapter_pauses_existing_job_processing(self):
        campaign = create_campaign(self.config)
        add_cycle(campaign, self._points(["1k"]))
        submitted = step(campaign)
        run = Path(read_json(campaign / "campaign.json")["cycles"][0]["points"][0]["run"])
        self.assertEqual(submitted["cycles"][0]["counts"], {"SUBMITTED": 1})
        adapter = self.root / "adapter.py"
        original = adapter.read_text(encoding="utf-8")
        adapter.write_text(original + "\n# modified\n", encoding="utf-8")
        blocked = step(campaign)
        self.assertEqual(blocked["status"], "NEEDS_ATTENTION")
        self.assertEqual(blocked["cycles"][0]["counts"], {"SUBMITTED": 1})
        self.assertFalse((run / "poll_count").exists())
        adapter.write_text(original, encoding="utf-8")
        self.assertEqual(step(campaign)["cycles"][0]["counts"], {"RUN": 1})

    def test_external_site_worker_uses_same_campaign_loop(self):
        cfg = read_json(self.config)
        cfg["simulation"] = {"backend": "external"}
        cfg["campaign"]["max_parallel"] = 1
        write_json(self.config, cfg)
        campaign = create_campaign(self.config)
        add_cycle(campaign, self._points(["1k", "2k"]))
        first = step(campaign)
        self.assertEqual(first["status"], "AWAITING_EXECUTION")
        self.assertEqual(first["cycles"][0]["counts"], {"QUEUED": 1, "STAGED": 1})
        self.assertEqual(step(campaign)["cycles"][0]["counts"], {"QUEUED": 1, "STAGED": 1})

        def verified_external_run(index, value):
            point = read_json(campaign / "campaign.json")["cycles"][0]["points"][index]
            run = Path(point["run"])
            net = read_json(run / "netlist_result.json")
            write_json(run / "submission_result.json", {"job_id": f"site-{index}"})
            transition(run, "SUBMITTED")
            transition(run, "DONE")
            transition(run, "RETRIEVED")
            write_json(run / "simulation_result.json", {
                "status": "DONE", "data": {"dc_A": value},
                "netlist_sha256": net["netlist_sha256"]})
            transition(run, "VERIFIED")

        verified_external_run(0, 1.0)
        verified_run = Path(read_json(campaign / "campaign.json")["cycles"][0]["points"][0]["run"])
        recovered = create_campaign(self.config)
        add_cycle(recovered, self._points(["1k"]))
        with self.assertRaisesRegex(ValueError, "audit reason"):
            attach_verified_run(recovered, "p1", verified_run, "")
        self.assertEqual(attach_verified_run(recovered, "p1", verified_run,
                                            "Re-evaluate analysis without resubmitting")["cycles"][0]
                         ["counts"], {"VERIFIED": 1})
        self.assertEqual(step(recovered)["status"], "REVIEW")
        second = step(campaign)
        self.assertEqual(second["status"], "AWAITING_EXECUTION")
        self.assertEqual(second["cycles"][0]["counts"], {"ANALYZED": 1, "STAGED": 1})
        verified_external_run(1, 2.0)
        self.assertEqual(step(campaign)["status"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
