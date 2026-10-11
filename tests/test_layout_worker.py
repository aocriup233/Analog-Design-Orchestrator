import copy
from unittest.mock import patch

from analog_agent.core import read_json, write_json
from analog_agent.layout_constraints import digest
from analog_agent.layout_floorplan import _frozen
from analog_agent.layout_runs import layout_context, layout_lock, prepare_layout
from analog_agent.layout_routing import routing_context
from analog_agent.layout_worker import publish, route, snapshot
from analog_agent.state import status
from test_layout_floorplan import LayoutFloorplanFixture


class LayoutWorkerFixture(LayoutFloorplanFixture):
    def setUp(self):
        super().setUp()
        adapter = self.root / "private/layout/operations.py"
        adapter.write_text("def dispatch(context): raise NotImplementedError('audited adapter required')\n", encoding="utf-8")
        config = read_json(self.layout_config)
        config["layout"]["bridge"]["operations"] = "private/layout/operations.py:dispatch"
        config["layout"]["verification_adapter"] = "private/layout/operations.py:dispatch"
        config["layout"]["tech_summary"] = {"layers": {"metal": {"min_width_nm": 10, "min_spacing_nm": 5}}, "vias": {}}
        write_json(self.layout_config, config)
        self.run = prepare_layout(self.source)
        self.freeze()
        cfg, _ = layout_context(self.run)
        with layout_lock(self.run) as session:
            _, _, context = routing_context(self.run, session)
        self.plan = {"schema_version": 1, "inputs": context["inputs"], "net_order": context["allowed_nets"],
                     "routes": [], "symmetric_pairs": [], "violation_ids": []}
        from analog_agent.core import update_status
        update_status(self.run, status(self.run), active_routing_plan=publish(self.run, "routing_proposals", self.plan))
        self.data = {"schema_version": 1, "target": {"library": cfg["library"], "cell": cfg["cell"], "view": "layout"},
                     "saved": True, "placement_sha256": _frozen(self.run, cfg)["placement_sha256"],
                     "geometry": [], "vias": [], "nets": [], "pins": [], "hierarchy": [], "params": {},
                     "technology_sha256": digest({}), "declared_inputs_sha256": digest({}), "protected_sha256": digest({})}
        self.data["sha256"] = digest(self.data)
        self.calls = []

    def dispatch(self, run, operation, payload):
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == "read_layout_snapshot":
            return copy.deepcopy(self.data)
        if operation == "read_terminals":
            return {"cellview_sha256": self.data["sha256"], "routed_nets": self.plan["net_order"], "unrouted_nets": [], "warnings": []}
        if operation == "save_target":
            return {"saved": True, "cellview_sha256": self.data["sha256"]}
        raise AssertionError(operation)


class LayoutWorkerTest(LayoutWorkerFixture):
    def test_fake_routing_single_lock_no_placement_operation(self):
        with patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            result = route(self.run)
        self.assertEqual(status(self.run), "ROUTED")
        self.assertEqual(read_json(self.run / result["path"])["cellview_sha256"], self.data["sha256"])
        self.assertTrue(all(op in {"read_layout_snapshot", "save_target", "read_terminals"} for op, _ in self.calls))
        self.assertFalse((self.run / ".layout.lock").exists())

    def test_proposal_injection_rejected_before_bridge(self):
        self.plan["shell"] = "forbidden"
        from analog_agent.core import update_status
        update_status(self.run, status(self.run), active_routing_plan=publish(self.run, "routing_proposals", self.plan))
        with patch("analog_agent.layout_worker.execute") as bridge, self.assertRaises(ValueError):
            route(self.run)
        bridge.assert_not_called()
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")

    def test_snapshot_tamper_and_timeout_require_attention(self):
        self.data["sha256"] = "0" * 64
        with patch("analog_agent.layout_worker.execute", side_effect=self.dispatch), self.assertRaises(ValueError):
            route(self.run)
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")

    def test_moved_manual_floorplan_blocks_before_routing(self):
        self.floorplan["instances"][0]["x"] = 0.001
        with patch("analog_agent.layout_worker.execute") as bridge, self.assertRaisesRegex(ValueError, "FLOORPLAN_PENDING"):
            route(self.run)
        bridge.assert_not_called()
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_operations_and_missing_adapter_fail_closed(self):
        from analog_agent.layout_executor import execute
        with self.assertRaisesRegex(ValueError, "allowlist"):
            execute(self.run, "execute_skill", {"code": "forbidden"})
        cfg, _ = layout_context(self.run)
        self.data["saved"] = False
        self.data["sha256"] = digest({k: v for k, v in self.data.items() if k != "sha256"})
        with patch("analog_agent.layout_worker.execute", side_effect=self.dispatch), self.assertRaises(ValueError):
            snapshot(self.run, cfg)
