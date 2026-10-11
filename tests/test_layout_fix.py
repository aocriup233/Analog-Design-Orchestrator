import copy
import io
import sys
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import read_json, update_status
from analog_agent.layout_fix import apply_fix, prepare_fix
from analog_agent.layout_routing import routing_context, propose_routing
from analog_agent.layout_runs import layout_lock
from analog_agent.layout_verification import verify
from analog_agent.layout_worker import publish
from analog_agent.state import status
from test_layout_verification import VerificationFixture


class LayoutFixTest(VerificationFixture):
    def test_placement_failure_at_fix_limit_preserves_pending(self):
        self.fail_drc()
        self.proposal()
        update_status(self.run, status(self.run), fix_iterations=2)
        self.repair_class = "placement"
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            result = apply_fix(self.run)
        self.assertEqual(result["status"], "FLOORPLAN_PENDING")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def fail_drc(self):
        self.stage_clean["drc"] = False
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            verify(self.run)

    def proposal(self):
        with layout_lock(self.run) as session:
            fix = prepare_fix(self.run, session)
            _, _, context = routing_context(self.run, session, fix=fix)
        plan = copy.deepcopy(self.plan)
        plan["inputs"] = context["inputs"]
        plan["violation_ids"] = context["violation_ids"]
        plan["routes"] = [{"id": "ground", "net": "0", "layer": "metal", "width_nm": 10, "spacing_nm": 5,
                           "shielding": None, "guide_points": [[0, 0], [100, 0]], "vias": []}]
        reference = publish(self.run, "routing_proposals", plan)
        update_status(self.run, status(self.run), active_fix=fix, active_routing_plan=reference)
        return reference

    def test_explicit_fix_routes_only_owned_ids_and_reverifies(self):
        self.fail_drc()
        with self.assertRaisesRegex(ValueError, "Review"):
            apply_fix(self.run)
        self.proposal()
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            first = apply_fix(self.run)
        self.assertEqual(first["status"], "DRC_DONE")
        report = read_json(self.run / read_json(self.run / "task.json")["active_routing_result"]["path"])
        owned = report["owned_routes"][0]["id"]
        self.proposal()
        self.stage_clean["drc"] = True
        with patch("analog_agent.layout_verification.execute", side_effect=self.dispatch), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            second = apply_fix(self.run)
        self.assertEqual(second["status"], "LAYOUT_VERIFIED")
        deletes = [payload["route_ids"] for op, payload in self.calls if op == "remove_owned_routes"]
        self.assertEqual(deletes, [[], [owned]])
        self.assertEqual(read_json(self.run / "task.json")["fix_iterations"], 2)
        self.assertEqual(len(self.data["geometry"]), 1)

    def test_fix_limit_and_scope(self):
        self.fail_drc()
        reference = self.proposal()
        plan = read_json(self.run / reference["path"])
        plan["net_order"] = ["other"]
        update_status(self.run, status(self.run), active_routing_plan=publish(self.run, "routing_proposals", plan))
        with patch("analog_agent.layout_worker.execute") as bridge, self.assertRaises(ValueError):
            apply_fix(self.run)
        bridge.assert_not_called()
        self.assertEqual(status(self.run), "DRC_DONE")
        update_status(self.run, status(self.run), fix_iterations=3)
        with self.assertRaisesRegex(ValueError, "Maximum"):
            apply_fix(self.run)
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")

    def test_cli_verification_failure_nonzero(self):
        with patch.object(sys, "argv", ["analog-agent", "layout", "verify", str(self.run)]), \
             patch("analog_agent.cli.verify_layout", return_value={"clean": False, "status": "DRC_DONE"}), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(), 1)
