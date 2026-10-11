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
        config["layout"]["lvs_source"] = {"generator": "private/layout/operations.py:dispatch",
              "normalizer": "private/layout/operations.py:dispatch", "top": "top", "dependencies": []}
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
        if operation in {"append_path", "append_via"}:
            self.data["geometry" if operation == "append_path" else "vias"].append(copy.deepcopy(payload))
            self.data["sha256"] = digest({k: v for k, v in self.data.items() if k != "sha256"})
            return {"route_id": payload["route_id"], "net": payload["net"], "created": True}
        if operation == "remove_owned_routes":
            for key in ("geometry", "vias"):
                self.data[key] = [g for g in self.data[key] if g["route_id"] not in payload["route_ids"]]
            self.data["sha256"] = digest({k: v for k, v in self.data.items() if k != "sha256"})
            return {"removed_route_ids": payload["route_ids"]}
        raise AssertionError(operation)


class LayoutWorkerTest(LayoutWorkerFixture):
    def test_real_stub_isolated_result_channel_fails_closed(self):
        from analog_agent.layout_executor import execute
        with self.assertRaisesRegex(ValueError, "not implemented"):
            execute(self.run, "read_layout_snapshot", {})
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")
        results = list((self.run / "execution").glob("*/result.json"))
        self.assertEqual(len(results), 1)
        self.assertEqual(read_json(results[0]), {"status": "unsupported"})

    def test_timeout_second_communicate_is_always_timeout_error(self):
        import subprocess
        from analog_agent.layout_executor import execute
        with patch("analog_agent.layout_executor.subprocess.Popen") as factory, \
             patch("analog_agent.layout_executor._kill_reader") as kill:
            process = factory.return_value
            process.communicate.side_effect = subprocess.TimeoutExpired("fixture", 1)
            with self.assertRaises(TimeoutError):
                execute(self.run, "read_layout_snapshot", {})
            kill.assert_called_once_with(process)
            self.assertEqual(process.communicate.call_count, 2)
            command = factory.call_args.args[0]
            self.assertEqual(command[-1].split("result.json")[-1], "")

    def test_planner_has_json_context_and_cannot_execute(self):
        from analog_agent.layout_routing import propose_routing
        def planner(role, project, context_path, output_path, directory):
            context = read_json(context_path)
            self.assertEqual(role, "routing")
            self.assertNotIn("verification_adapter", context)
            self.assertNotIn("private", context)
            write_json(output_path, {**self.plan, "inputs": context["inputs"]})
        with patch("analog_agent.workflow.invoke_json_role", side_effect=planner), \
             patch("analog_agent.layout_worker.execute") as bridge:
            result = propose_routing(self.run)
        bridge.assert_not_called()
        self.assertEqual(result["status"], "FLOORPLAN_FROZEN")

    def test_owned_path_mutation_and_full_hash_readback(self):
        from analog_agent.core import update_status
        self.plan["routes"] = [{"id": "ground", "net": "0", "layer": "metal", "width_nm": 10,
                               "spacing_nm": 5, "shielding": None, "guide_points": [[0, 0], [100, 0]], "vias": []}]
        update_status(self.run, status(self.run), active_routing_plan=publish(self.run, "routing_proposals", self.plan))
        original = self.data["sha256"]
        with patch("analog_agent.layout_worker.execute", side_effect=self.dispatch):
            result = route(self.run)
        report = read_json(self.run / result["path"])
        self.assertNotEqual(original, report["cellview_sha256"])
        self.assertEqual(report["owned_routes"][0]["id"], self.data["geometry"][0]["route_id"])
        self.assertEqual(report["owned_routes"][0]["net"], "0")

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
