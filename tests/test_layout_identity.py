import copy
import io
import json
import sys
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import read_json, sha256, write_json
from analog_agent.layout_bridge import ReaderStateError, read_placement
from analog_agent.layout_floorplan import (floorplan_export, floorplan_freeze, instance_identifier,
    params_sha256, placement_sha256, validate_floorplan)
from analog_agent.layout_runs import layout_context, prepare_layout
from analog_agent.state import status
from test_layout_floorplan import LayoutFloorplanFixture


class LayoutIdentityTest(LayoutFloorplanFixture):
    def test_um_nm_identity_including_pins_labels_boundary(self):
        cfg, _ = layout_context(self.run)
        self.floorplan["pins"] = [{"name": "IN", "layer": "metal", "bbox": [[0, 0], [1, 2]]}]
        self.floorplan["labels"] = [{"text": "IN", "layer": "metal", "bbox": [[0, 0], [1, 2]]}]
        self.floorplan["pr_boundary"] = [[0, 0], [1, 0], [1, 2], [0, 2]]
        nm = copy.deepcopy(self.floorplan)
        nm["units"] = "nm"
        for item in nm["instances"]:
            item["x"] *= 1000
            item["y"] *= 1000
        for item in nm["instances"] + nm["pins"] + nm["labels"]:
            item["bbox"] = [[v * 1000 for v in point] for point in item["bbox"]]
        nm["pr_boundary"] = [[v * 1000 for v in point] for point in nm["pr_boundary"]]
        one, two = validate_floorplan(self.floorplan, cfg), validate_floorplan(nm, cfg)
        self.assertEqual(placement_sha256(one), placement_sha256(two))
        self.assertIs(type(one["instances"][0]["x"]), int)
        self.assertEqual(validate_floorplan(one, cfg), one)

    def test_grid_float_noise_and_origin_bounds(self):
        cfg, _ = layout_context(self.run)
        cfg["grid_nm"] = 2
        self.floorplan["instances"][0]["x"] = 0.0020000000000001
        self.assertEqual(validate_floorplan(self.floorplan, cfg)["instances"][0]["x"], 2)
        self.floorplan["instances"][0]["x"] = 0.001
        with self.assertRaisesRegex(ValueError, "Off-grid"):
            validate_floorplan(self.floorplan, cfg)
        self.floorplan["instances"][0]["x"] = -0.002
        with self.assertRaisesRegex(ValueError, "outside bbox: I0"):
            validate_floorplan(self.floorplan, cfg)
        cfg["bbox_tolerance_nm"] = 2
        validate_floorplan(self.floorplan, cfg)

    def test_parameter_change_without_moving_invalidates_freeze(self):
        self.freeze()
        item = self.floorplan["instances"][0]
        item["params"] = {"generic_parameter": "new"}
        item["params_sha256"] = params_sha256(item["params"])
        result = floorplan_export(self.run)
        self.assertTrue(result["changed"])
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_every_added_identity_field_changes_hash(self):
        cfg, _ = layout_context(self.run)
        baseline = placement_sha256(validate_floorplan(self.floorplan, cfg))
        for field, value in (("pins", [{"name": "IN", "layer": "metal", "bbox": [[0, 0], [1, 1]]}]),
                             ("labels", [{"text": "IN", "layer": "metal", "bbox": [[0, 0], [1, 1]]}]),
                             ("pr_boundary", [[0, 0], [1, 0], [1, 1]])):
            candidate = copy.deepcopy(self.floorplan)
            candidate[field] = value
            self.assertNotEqual(baseline, placement_sha256(validate_floorplan(candidate, cfg)))

    def test_canonical_hierarchical_iteration_names(self):
        self.assertEqual(instance_identifier("X0/M1<0:3>"), "X0%2FM1%3C0%3A3%3E")
        self.assertEqual(instance_identifier("X0%2FM1%3C0%3A3%3E"), "X0%2FM1%3C0%3A3%3E")
        for value in ("/M1", "X0//M1", "X0/../M1", "M1<00:3>", "M1<0:3>;sh()", "M1%3c0%3e", "M1%252F", "M1\\M2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                instance_identifier(value)
        cfg, _ = layout_context(self.run)
        self.floorplan["instances"][0]["instance"] = "X0/M1<0:3>"
        self.assertEqual(validate_floorplan(self.floorplan, cfg)["instances"][0]["instance"], "X0%2FM1%3C0%3A3%3E")

    def test_leading_digits_and_all_offending_master_names(self):
        cfg, _ = layout_context(self.run)
        item = self.floorplan["instances"][0]
        item.update(instance="1instance")
        item["master"].update(library="2lib", cell="3cell")
        with self.assertRaises(ValueError) as raised:
            validate_floorplan(self.floorplan, cfg)
        for value in ("1instance", "2lib", "3cell"):
            self.assertIn(value, str(raised.exception))
        cfg["allow_leading_digit"] = True
        validate_floorplan(self.floorplan, cfg)
        item["master"]["view"] = "1view"
        with self.assertRaises(ValueError):
            validate_floorplan(self.floorplan, cfg)

    def test_input_errors_leave_state_and_crash_requires_attention(self):
        before = sha256(self.run / "task.json")
        self.floorplan["cellview"]["cell"] = "wrong"
        with self.assertRaisesRegex(ValueError, "target"):
            floorplan_export(self.run)
        self.assertEqual(sha256(self.run / "task.json"), before)
        self.floorplan["cellview"]["cell"] = "top"
        self.floorplan["instances"][0]["orientation"] = "invalid"
        with self.assertRaises(ValueError):
            floorplan_export(self.run)
        self.assertEqual(status(self.run), "LAYOUT_PREPARED")
        self.mock_reader.side_effect = NotImplementedError("known unsupported adapter")
        with self.assertRaises(NotImplementedError):
            floorplan_export(self.run)
        self.assertEqual(status(self.run), "LAYOUT_PREPARED")
        self.mock_reader.side_effect = ReaderStateError("unknown remote state")
        with self.assertRaises(ReaderStateError):
            floorplan_export(self.run)
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")

    def test_missing_reader_is_preflight_error_without_call(self):
        cfg = read_json(self.layout_config)
        del cfg["layout"]["bridge"]
        write_json(self.layout_config, cfg)
        run = prepare_layout(self.source)
        with self.assertRaisesRegex(ValueError, "not configured"):
            floorplan_export(run)
        self.mock_reader.assert_not_called()
        self.assertEqual(status(run), "LAYOUT_PREPARED")

    def test_real_stub_error_preserves_state_with_private_log(self):
        self.reader.stop()
        path = self.root / "private/layout/reader.py"
        path.write_text("def read(context): raise NotImplementedError('PRIVATE DETAILS')\n", encoding="utf-8")
        run = prepare_layout(self.source)
        with self.assertRaises(ValueError) as raised:
            floorplan_export(run)
        self.assertEqual(status(run), "LAYOUT_PREPARED")
        self.assertNotIn("PRIVATE DETAILS", str(raised.exception))
        self.assertIn("private_logs/", str(raised.exception))

    def test_freeze_cli_nonzero_when_live_changed(self):
        self.pending()
        floorplan_export(self.run)
        self.floorplan["instances"][0]["y"] = 0.5
        output = io.StringIO()
        with patch.object(sys, "argv", ["analog-agent", "layout", "floorplan-freeze", str(self.run), "--reason", "reviewed"]), \
                patch("sys.stdout", output):
            self.assertNotEqual(cli.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "FLOORPLAN_PENDING")
