from unittest.mock import patch

from analog_agent.core import read_json, sha256, write_json
from analog_agent.input_gate import InputMismatch, verify_inputs
from analog_agent.layout_floorplan import floorplan_export, floorplan_freeze
from analog_agent.layout_worker import route
from analog_agent.layout_verification import verify
from test_layout_floorplan import LayoutFloorplanFixture


class LayoutInputGateTest(LayoutFloorplanFixture):
    def test_authorized_raw_cleanup_is_only_exempt_for_layout_link(self):
        from analog_agent.core import update_status
        from analog_agent.layout_runs import prepare_layout
        from analog_agent.state import transition
        raw = self.source / "raw" / "trace.txt"
        raw.parent.mkdir()
        raw.write_text("fake verified output\n")
        write_json(self.source / "retrieval_result.json", {"backend_data": {
            "artifacts": [{"path": str(raw), "sha256": sha256(raw)}]}})
        transition(self.source, "ANALYZED")
        update_status(self.source, "ANALYZED", raw_cleaned=True)
        raw.unlink()
        layout = prepare_layout(self.source)
        verify_inputs("layout_export", layout)
        with self.assertRaisesRegex(InputMismatch, "actual=MISSING"):
            verify_inputs("analysis", self.source)

    def test_configured_runset_is_pinned_before_first_verification(self):
        from pathlib import Path
        from analog_agent.layout_runs import prepare_layout
        cfg = read_json(self.layout_config)
        runset = Path(cfg["layout"]["drc"]["rule_dir"]) / "runset.txt"
        runset.write_text("generic runset\n")
        cfg["layout"]["drc"]["runset"] = str(runset)
        write_json(self.layout_config, cfg)
        self.run = prepare_layout(self.source)
        verify_inputs("layout_verify", self.run)
        runset.write_text("changed generic runset\n")
        before = (self.run / "task.json").read_bytes()
        with self.assertRaises(InputMismatch):
            verify_inputs("layout_verify", self.run)
        runset.unlink()
        with self.assertRaisesRegex(InputMismatch, "actual=MISSING"):
            verify_inputs("layout_verify", self.run)
        self.assertEqual((self.run / "task.json").read_bytes(), before)

    def test_frozen_user_source_tamper_is_preflight_error(self):
        user = self.root / "private/layout/constraints.json"
        write_json(user, {"schema_version": 1, "constraints": []})
        self.freeze()
        user.write_bytes(user.read_bytes() + b" ")
        before = (self.run / "task.json").read_bytes()
        self.mock_reader.reset_mock()
        with self.assertRaises(InputMismatch):
            floorplan_export(self.run)
        self.mock_reader.assert_not_called()
        self.assertEqual((self.run / "task.json").read_bytes(), before)

    def test_private_config_and_reader_fail_without_bridge_or_state_change(self):
        for filename in ("private/layout/config.json", "private/layout/reader.py"):
            with self.subTest(filename=filename):
                target = self.root / filename
                original = target.read_bytes()
                before = (self.run / "task.json").read_bytes()
                self.mock_reader.reset_mock()
                target.write_bytes(original + b" ")
                try:
                    with self.assertRaisesRegex(InputMismatch, "expected=.*actual="):
                        floorplan_export(self.run)
                    self.assertEqual((self.run / "task.json").read_bytes(), before)
                    self.mock_reader.assert_not_called()
                finally:
                    target.write_bytes(original)

    def test_every_layout_stage_rejects_tampered_freeze_without_transition(self):
        frozen = self.freeze()
        path = self.run / frozen["path"]
        path.write_bytes(path.read_bytes() + b" ")
        before = (self.run / "task.json").read_bytes()
        for function, args in ((floorplan_export, ()), (floorplan_freeze, ("review",)), (route, ()), (verify, ())):
            with self.subTest(stage=function.__name__):
                with patch("analog_agent.layout_worker.execute") as bridge, self.assertRaises(InputMismatch):
                    function(self.run, *args)
                bridge.assert_not_called()
                self.assertEqual((self.run / "task.json").read_bytes(), before)

    def test_external_consumed_rule_tamper_and_missing_file(self):
        cfg = read_json(self.layout_config)["layout"]
        from pathlib import Path
        rule = Path(cfg["drc"]["rule_dir"]) / "rules.txt"
        rule.write_text("generic numeric test rule\n")
        report = {"stage": "drc", "rule_sha256": {str(rule): sha256(rule)}}
        write_json(self.run / "drc_report.json", report)
        verify_inputs("layout_verify", self.run)
        rule.write_text("changed generic rule\n")
        before = (self.run / "task.json").read_bytes()
        with self.assertRaisesRegex(InputMismatch, "expected=.*actual="):
            verify_inputs("layout_verify", self.run)
        rule.unlink()
        with self.assertRaisesRegex(InputMismatch, "actual=MISSING"):
            verify_inputs("layout_verify", self.run)
        self.assertEqual((self.run / "task.json").read_bytes(), before)
