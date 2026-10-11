import io
import sys
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import read_json, sha256, write_json
from analog_agent.layout_floorplan import floorplan_export, floorplan_freeze, require_unchanged_floorplan
from analog_agent.layout_placement_check import routing_review, floorplan_constraints_check
from analog_agent.state import status
from test_layout_floorplan import LayoutFloorplanFixture


class RoutingReviewTest(LayoutFloorplanFixture):
    def test_missing_user_floorplan_member_blocks_before_live_capture(self):
        from analog_agent.core import create_run
        from analog_agent.netlist_agent import prepare
        from analog_agent.layout_runs import prepare_layout
        from test_layout_constraints import matching
        (self.root / "template.scs").write_text("I0 (A B) resistor r=1k\nI1 (A C) resistor r=1k\n", encoding="utf-8")
        self.source = create_run(self.config, 0)
        self.net = prepare(self.source)
        self.verify_source()
        self.run = prepare_layout(self.source)
        write_json(self.root / "private/layout/constraints.json", {"schema_version": 1, "constraints": [matching()]})
        self.pending()
        floorplan_export(self.run)
        self.mock_reader.reset_mock()
        with self.assertRaisesRegex(ValueError, "Orphan floorplan references"):
            floorplan_freeze(self.run, "incomplete manual placement")
        self.mock_reader.assert_not_called()
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def unreviewed(self, warn=False):
        if warn:
            write_json(self.root / "private/layout/constraints.json", {"schema_version": 1, "constraints": [
                {"id": "space", "kind": "keep_out", "source": "user", "rationale": "reserved",
                 "bbox_nm": [[0, 0], [1000, 2000]]}]})
        self.pending()
        floorplan_export(self.run)
        return floorplan_freeze(self.run, "human placement review")

    def test_warning_decision_required_and_bound_to_snapshot(self):
        frozen = self.unreviewed(True)
        with self.assertRaisesRegex(ValueError, "routing-review"):
            require_unchanged_floorplan(self.run, "routing")
        with self.assertRaisesRegex(ValueError, "proceed-with-warnings"):
            routing_review(self.run, "proceed", "reason alone insufficient")
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")
        with patch.object(sys, "argv", ["analog-agent", "layout", "routing-review", str(self.run),
                         "--decision", "proceed-with-warnings", "--reason", "explicitly accept current warnings"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(), 0)
        require_unchanged_floorplan(self.run, "routing")
        receipt = read_json(self.run / frozen["path"])
        checked = floorplan_constraints_check(self.run)
        self.assertEqual(receipt["placement_check"], {k: checked[k] for k in ("path", "sha256")})
        self.assertEqual(sha256(self.run / checked["path"]), checked["sha256"])
        ref = read_json(self.run / "task.json")["active_routing_review"]
        review = read_json(self.run / ref["path"])
        self.assertEqual(review["placement_check"], receipt["placement_check"])
        write_json(self.run / checked["path"], {})
        with self.assertRaises(ValueError):
            require_unchanged_floorplan(self.run, "routing")
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")

    def test_clean_decision_and_movement_invalidate_authority(self):
        self.unreviewed()
        with self.assertRaises(ValueError):
            require_unchanged_floorplan(self.run, "routing")
        routing_review(self.run, "proceed", "clean check reviewed")
        require_unchanged_floorplan(self.run, "routing")
        self.floorplan["instances"][0]["x"] = 0.001
        with self.assertRaises(ValueError):
            require_unchanged_floorplan(self.run, "routing")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        floorplan_freeze(self.run, "reviewed moved placement")
        with self.assertRaisesRegex(ValueError, "routing-review"):
            require_unchanged_floorplan(self.run, "routing")
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")
        routing_review(self.run, "proceed", "fresh placement decision")
        require_unchanged_floorplan(self.run, "routing")

    def test_review_tampering_is_not_a_new_decision(self):
        self.unreviewed()
        ref = routing_review(self.run, "proceed", "reviewed")
        write_json(self.run / ref["path"], {})
        with self.assertRaises(ValueError):
            require_unchanged_floorplan(self.run, "routing")
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")
