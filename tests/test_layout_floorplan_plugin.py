import copy
import io
import sys
from pathlib import Path
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import read_json, sha256, write_json
from analog_agent.layout_executor import call_adapter
from analog_agent.layout_floorplan import floorplan_freeze, placement_sha256, require_unchanged_floorplan, validate_floorplan
from analog_agent.layout_floorplan_plugin import floorplan_generate
from analog_agent.layout_placement_check import routing_review
from analog_agent.layout_runs import layout_context, prepare_layout
from analog_agent.state import status
from test_layout_floorplan import LayoutFloorplanFixture
from test_layout_floorplan_rules import rules


class FloorplanPluginFixture(LayoutFloorplanFixture):
    def configure(self, rule_document=None, code=None):
        cfg = read_json(self.layout_config)
        cfg["layout"]["floorplan"] = {"mode": "plugin", "generator": "private/layout/generator.py:generate"}
        write_json(self.layout_config, cfg)
        write_json(self.root / "private/layout/floorplan_rules.json", rule_document or rules())
        example = Path(__file__).resolve().parents[1] / "private.example/layout/floorplan_generator.py"
        (self.root / "private/layout/generator.py").write_text(code if code is not None else example.read_text(encoding="utf-8"), encoding="utf-8")
        self.run = prepare_layout(self.source)
        self.pending()
        self.placement = self.root / "private/layout/placement.json"
        write_json(self.placement, self.floorplan)


class FloorplanPluginTest(FloorplanPluginFixture):
    def test_echo_determinism_human_gate_and_live_check(self):
        self.configure()
        self.mock_reader.reset_mock()
        first = floorplan_generate(self.run, placement=self.placement)
        second = floorplan_generate(self.run, placement=self.placement)
        self.mock_reader.assert_not_called()
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertFalse(first["applied"])
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        cfg, _ = layout_context(self.run)
        candidate = read_json(self.run / "floorplan.json")
        self.assertEqual(candidate["source"], "plugin")
        self.assertEqual(placement_sha256(candidate), placement_sha256(validate_floorplan(self.floorplan, cfg)))
        frozen = floorplan_freeze(self.run, "reviewed echo already matching live placement")
        receipt = read_json(self.run / frozen["path"])
        self.assertEqual(receipt["generation"], second["generation"])
        self.assertIn("placement_check", receipt)
        with self.assertRaises(ValueError):
            require_unchanged_floorplan(self.run, "routing")
        routing_review(self.run, "proceed-with-warnings" if frozen["constraint_warnings"] else "proceed", "human routing release")
        require_unchanged_floorplan(self.run, "routing")

    def test_proposal_cannot_freeze_if_not_applied_by_user(self):
        self.configure()
        changed = copy.deepcopy(self.floorplan)
        changed["instances"][0]["x"] = 1
        changed["instances"][0]["bbox"] = [[1, 0], [2, 2]]
        write_json(self.placement, changed)
        floorplan_generate(self.run, placement=self.placement)
        result = floorplan_freeze(self.run, "reviewed but live differs")
        self.assertEqual(result["status"], "FLOORPLAN_PENDING")
        self.assertTrue(result["changed"])
        self.assertFalse(read_json(self.run / "task.json").get("active_floorplan_freeze"))

    def test_rule_document_passed_unchanged_and_orphans_fail(self):
        data = rules()
        data["rows"] = [{"id": "row", "bbox_nm": [[0, 0], [100, 100]], "instances": ["MISSING"], "orientation": "R0"}]
        self.configure(data)
        cfg, _ = layout_context(self.run)
        candidate = {**validate_floorplan(self.floorplan, cfg), "source": "plugin", "plugin_sha256": sha256(self.root / "private/layout/generator.py")}
        with patch("analog_agent.layout_floorplan_plugin.execute", return_value=candidate) as generator, self.assertRaisesRegex(ValueError, "MISSING"):
            floorplan_generate(self.run)
        self.assertEqual(generator.call_args.args[2]["rules"], data)
        self.assertNotIn("drc", generator.call_args.args[2])
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        self.assertFalse((self.run / "floorplan.json").exists())

    def test_changed_generation_inputs_block_freeze(self):
        self.configure()
        floorplan_generate(self.run, placement=self.placement)
        write_json(self.placement, {})
        with self.assertRaises(ValueError):
            floorplan_freeze(self.run, "reviewed")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_missing_input_or_tampered_receipt_blocks_freeze(self):
        self.configure()
        result = floorplan_generate(self.run, placement=self.placement)
        write_json(self.run / result["generation"]["path"], {})
        with self.assertRaises(ValueError):
            floorplan_freeze(self.run, "reviewed")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        self.configure()
        floorplan_generate(self.run, placement=self.placement)
        self.placement.unlink()
        with self.assertRaises(ValueError):
            floorplan_freeze(self.run, "reviewed")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_forged_provenance_and_path_escape_rejected(self):
        self.configure()
        cfg, _ = layout_context(self.run)
        wrong = {**validate_floorplan(self.floorplan, cfg), "source": "plugin", "plugin_sha256": "0" * 64}
        with patch("analog_agent.layout_floorplan_plugin.execute", return_value=wrong), self.assertRaises(ValueError):
            floorplan_generate(self.run)
        with self.assertRaises(ValueError):
            floorplan_generate(self.run, placement=Path("project.json"))
        with self.assertRaises(ValueError):
            floorplan_generate(self.run, placement=Path("../outside.json"))
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_generator_uses_central_plugin_callable_policy(self):
        self.configure(code="from os import system as generate\n")
        from analog_agent.layout_adapter_context import adapter_context
        with self.assertRaisesRegex(ValueError, "Forbidden"):
            call_adapter(self.run, {"operation": "generate_floorplan", "payload": {},
                                    "adapter_context": adapter_context(self.run, self.run, self.run)})

    def test_manual_default_and_draft_gate(self):
        self.pending()
        with self.assertRaisesRegex(ValueError, "plugin mode"):
            floorplan_generate(self.run)
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        self.configure()
        new_run = prepare_layout(self.source)
        with self.assertRaisesRegex(ValueError, "constraints gate"):
            floorplan_generate(new_run)

    def test_cli_generation_dispatch(self):
        with patch.object(sys, "argv", ["analog-agent", "layout", "floorplan-generate", str(self.run), "--placement", "private/layout/placement.json"]), patch("analog_agent.cli.floorplan_generate", return_value={"status": "FLOORPLAN_PENDING", "applied": False}) as generate, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(), 0)
        generate.assert_called_once_with(self.run, placement=Path("private/layout/placement.json"))
