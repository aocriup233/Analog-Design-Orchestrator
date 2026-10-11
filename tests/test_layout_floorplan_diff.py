import copy
import io
import sys
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import sha256, read_json
from analog_agent.layout_floorplan import floorplan_freeze, floorplan_export
from analog_agent.layout_floorplan_diff import floorplan_diff
from analog_agent.layout_floorplan_plugin import floorplan_generate
from analog_agent.state import status
from test_layout_floorplan_plugin import FloorplanPluginFixture


class FloorplanDiffTest(FloorplanPluginFixture):
    def test_moves_rotation_missing_extra_and_complete_identity(self):
        self.configure()
        floorplan_generate(self.run, placement=self.placement)
        before = sha256(self.run / "floorplan.json")
        self.floorplan["instances"][0].update(x=1, orientation="R90", bbox=[[1, 0], [2, 2]])
        result = floorplan_diff(self.run)
        change = result["differences"][0]
        self.assertEqual(change["target_origin_nm"], [0, 0])
        self.assertEqual(change["target_orientation"], "R0")
        self.assertFalse(result["empty"])
        self.assertEqual(sha256(self.run / "floorplan.json"), before)
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        self.floorplan["instances"][0]["instance"] = "EXTRA"
        result = floorplan_diff(self.run)
        self.assertEqual({d["action"] for d in result["differences"]}, {"review_missing_instance", "review_extra_instance"})

    def test_repeated_empty_diff_preserves_candidate_after_failed_freeze(self):
        self.configure()
        floorplan_generate(self.run, placement=self.placement)
        original = copy.deepcopy(self.floorplan)
        self.floorplan["instances"] = []
        self.assertTrue(floorplan_freeze(self.run, "reviewed")["changed"])
        self.assertFalse(floorplan_diff(self.run)["empty"])
        self.floorplan = original
        self.assertEqual(floorplan_diff(self.run), floorplan_diff(self.run))
        self.assertTrue(floorplan_diff(self.run)["empty"])
        frozen = floorplan_freeze(self.run, "live now matches the original candidate")
        self.assertEqual(frozen["status"], "FLOORPLAN_FROZEN")
        self.assertEqual(read_json(self.run / frozen["path"])["floorplan"]["source"], "plugin")

    def test_pins_and_manual_comparison(self):
        self.pending()
        floorplan_export(self.run)
        self.floorplan["pins"] = [{"name": "IN", "layer": "metal", "bbox": [[0, 0], [1, 1]]}]
        self.assertEqual(floorplan_diff(self.run)["differences"][0]["kind"], "pins")

    def test_cli_nonempty_exit(self):
        with patch.object(sys, "argv", ["analog-agent", "layout", "floorplan-diff", str(self.run)]), patch("analog_agent.cli.floorplan_diff", return_value={"empty": False}), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(), 1)
