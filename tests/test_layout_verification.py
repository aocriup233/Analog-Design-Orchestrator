import copy
import json
from pathlib import Path
from unittest.mock import patch

from analog_agent.core import read_json, sha256, write_json
from analog_agent.layout_verification import parse_report, verify
from analog_agent.layout_worker import route
from analog_agent.state import status
from test_layout_worker import LayoutWorkerFixture


class VerificationParserTest(__import__("unittest").TestCase):
    def test_fixture_reports_and_truncated_counts(self):
        root = Path(__file__).parent / "fixtures"
        self.assertFalse(parse_report((root / "layout_drc_normalized.json").read_bytes(), stage="drc", tool="calibre")["clean"])
        self.assertTrue(parse_report((root / "layout_lvs_normalized.json").read_bytes(), stage="lvs", tool="pegasus")["clean"])
        document = read_json(root / "layout_drc_normalized.json")
        for key, value in (("completed", False), ("violations", []), ("counts_by_rule", {"spacing": -1}), ("lvs_match", True)):
            with self.assertRaises(ValueError):
                parse_report(json.dumps({**document, key: value}).encode(), stage="drc", tool="calibre")


class VerificationFixture(LayoutWorkerFixture):
    def setUp(self):
        super().setUp()
        self.stage_clean = {"drc": True, "lvs": True}
        cfg = read_json(self.layout_config)["layout"]
        self.rule = Path(cfg["drc"]["rule_dir"]) / "fixture.rules"
        self.rule.write_text("opaque test fixture, never sent to planner", encoding="utf-8")
        with patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            route(self.run)

    def dispatch(self, run, operation, payload):
        if operation == "export_verification_snapshot":
            path = run / payload["destination"] / "snapshot.json"
            write_json(path, self.data)
            return {"cellview_sha256": self.data["sha256"], "files": {"snapshot.json": sha256(path)}}
        if operation == "verify":
            if payload["phase"] == "inputs":
                return {"used_files": [str(self.rule)], "version": "fixture-1"}
            stage = payload["stage"]
            clean = self.stage_clean[stage]
            document = {"schema_version": 1, "tool": "calibre", "stage": stage, "completed": True,
                        "counts_by_rule": {} if clean else {"spacing": 1},
                        "violations": [] if clean else [{"id": "v1", "rule": "spacing", "layer": "metal", "bbox": [[0, 0], [10, 10]], "message": "fixture", "nets": ["0"]}],
                        "lvs_match": None if stage == "drc" else clean}
            path = run / payload["output_dir"] / "normalized.json"
            write_json(path, document)
            return {"tool": "calibre", "version": "fixture-1", "exit_code": 0,
                    "cellview_sha256": self.data["sha256"], "used_files": [str(self.rule)],
                    "report": "normalized.json", "raw_reports": {"normalized.json": sha256(path)}}
        return super().dispatch(run, operation, payload)


class LayoutVerificationTest(VerificationFixture):
    def test_human_routing_review_tamper_blocks_verification(self):
        reference = read_json(self.run / "task.json")["active_routing_review"]
        write_json(self.run / reference["path"], {"decision": "changed"})
        with patch("analog_agent.layout_verification.execute") as tool, self.assertRaises(ValueError):
            verify(self.run)
        tool.assert_not_called()
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")

    def test_same_snapshot_clean_drc_lvs_gate(self):
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            result = verify(self.run)
        self.assertEqual(result["status"], "LAYOUT_VERIFIED")
        drc, lvs = (read_json(self.run / f"{stage}_report.json") for stage in ("drc", "lvs"))
        self.assertEqual(drc["export"], lvs["export"])
        self.assertEqual(drc["cellview_sha256"], lvs["cellview_sha256"])
        self.assertEqual(drc["rule_sha256"][str(self.rule)], sha256(self.rule))

    def test_drc_failure_stops_before_lvs(self):
        self.stage_clean["drc"] = False
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            result = verify(self.run)
        self.assertFalse(result["clean"])
        self.assertEqual(status(self.run), "DRC_DONE")
        self.assertFalse((self.run / "lvs_report.json").exists())

    def test_lvs_failure_not_verified(self):
        self.stage_clean["lvs"] = False
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            result = verify(self.run)
        self.assertEqual(result["failed_stage"], "lvs")
        self.assertEqual(status(self.run), "LVS_DONE")
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            repeated = verify(self.run)
        self.assertEqual(repeated["failed_stage"], "lvs")
        self.assertEqual(status(self.run), "LVS_DONE")

    def test_rule_changed_during_tool_execution_requires_attention(self):
        original = self.dispatch
        def mutate(run, operation, payload):
            result = original(run, operation, payload)
            if operation == "verify" and payload["phase"] == "run":
                self.rule.write_text("changed", encoding="utf-8")
            return result
        with patch("analog_agent.layout_verification.execute", side_effect=mutate), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch), self.assertRaises(ValueError):
            verify(self.run)
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")

    def test_external_workdir_collision_never_reused(self):
        cfg = read_json(self.layout_config)["layout"]
        (Path(cfg["drc"]["run_dir"]) / self.run.name).mkdir()
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch), self.assertRaises(FileExistsError):
            verify(self.run)
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")
