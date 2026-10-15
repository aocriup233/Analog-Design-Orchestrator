import copy
import io
import json
import sys
import unittest
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import read_json, sha256, write_json
from analog_agent.layout_bridge import adapter_read, read_placement
from analog_agent.layout_floorplan import (floorplan_export, floorplan_freeze,
    params_sha256, placement_sha256, require_unchanged_floorplan, validate_floorplan)
from analog_agent.layout_runs import layout_context, prepare_layout
from analog_agent.state import status, transition
from test_layout_runs import LayoutRunsFixture


class LayoutFloorplanFixture(LayoutRunsFixture):
    # Reuse fixture construction only, not the parent test methods.
    def setUp(self):
        super().setUp()
        self.verify_source()
        reader = self.root / "private/layout/reader.py"
        reader.write_text("def read(context): return {}\n", encoding="utf-8")
        cfg = read_json(self.layout_config)
        cfg["layout"]["bridge"] = {"reader": "private/layout/reader.py:read"}
        write_json(self.layout_config, cfg)
        self.run = prepare_layout(self.source)
        self.floorplan = {"schema_version": 2, "source": "manual", "units": "um", "pins": [], "labels": [], "pr_boundary": [],
            "cellview": {"library": "worklib", "cell": "top", "view": "layout", "path": "worklib/top/layout"},
            "export_timestamp": "2026-01-01T00:00:00+00:00", "instances": [
                {"instance": "I0", "master": {"library": "generic", "cell": "device", "view": "layout"},
                 "x": 0, "y": 0, "orientation": "R0", "bbox": [[0, 0], [1, 2]],
                 "params": {}, "params_sha256": params_sha256({})}]}
        self.reader = patch("analog_agent.layout_floorplan.layout_bridge.read_placement",
                            side_effect=lambda *args: copy.deepcopy(self.floorplan))
        self.mock_reader = self.reader.start()
        self.addCleanup(self.reader.stop)

    def pending(self):
        from analog_agent.layout_constraints import constraints_draft, constraints_freeze
        constraints_draft(self.run)
        constraints_freeze(self.run, "reviewed fixture constraints")

    def freeze(self):
        self.pending()
        floorplan_export(self.run)
        result = floorplan_freeze(self.run, "reviewed manual fixture placement")
        from analog_agent.layout_placement_check import routing_review
        routing_review(self.run, "proceed-with-warnings" if result["constraint_warnings"] else "proceed",
                       "explicit fixture human routing decision")
        return result


class LayoutFloorplanTest(LayoutFloorplanFixture):
    def test_canonical_timestamp_order_and_float_hashing(self):
        cfg, _ = layout_context(self.run)
        original = validate_floorplan(self.floorplan, cfg)
        changed = copy.deepcopy(self.floorplan)
        changed["export_timestamp"] = "later"
        changed["instances"][0]["x"] = -0.0
        self.assertEqual(placement_sha256(original), placement_sha256(validate_floorplan(changed, cfg)))
        first = floorplan_export(self.run)
        second = floorplan_export(self.run)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["placement_sha256"], second["placement_sha256"])

    def test_freeze_gate_and_unchanged_checkpoints(self):
        floorplan_export(self.run)
        with self.assertRaisesRegex(ValueError, "constraints human gate"):
            floorplan_freeze(self.run, "reviewed")
        self.assertEqual(status(self.run), "LAYOUT_PREPARED")
        frozen = self.freeze()
        self.assertEqual(sha256(self.run / frozen["path"]), frozen["sha256"])
        require_unchanged_floorplan(self.run, "routing")
        transition(self.run, "ROUTED")
        require_unchanged_floorplan(self.run, "verification")
        self.assertEqual(status(self.run), "ROUTED")
        self.assertFalse((self.run / ".layout.lock").exists())

    def test_check_while_holding_one_lock_and_clone_freeze(self):
        from analog_agent.layout_runs import clone_layout, layout_lock
        frozen = self.freeze()
        with layout_lock(self.run) as session:
            require_unchanged_floorplan(self.run, "routing", session=session)
            with self.assertRaisesRegex(ValueError, "locked"):
                require_unchanged_floorplan(self.run, "routing")
        before = sha256(self.run / "task.json")
        clone = clone_layout(self.run)
        self.assertEqual(status(clone), "LAYOUT_PREPARED")
        self.assertEqual(before, sha256(self.run / "task.json"))
        ref = read_json(clone / "task.json")["inherited_floorplan_freeze"]
        self.assertEqual(ref["sha256"], frozen["sha256"])
        self.assertEqual(read_json(clone / ref["path"]), read_json(self.run / frozen["path"]))
        write_json(self.run / frozen["path"], {})
        with self.assertRaises(ValueError):
            clone_layout(self.run)

    def test_moved_instance_invalidates_freeze_and_preserves_old_receipt(self):
        frozen = self.freeze()
        self.floorplan["instances"][0]["x"] = 0.001
        with self.assertRaisesRegex(ValueError, "FLOORPLAN_PENDING"):
            require_unchanged_floorplan(self.run, "routing")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        self.assertIsNone(read_json(self.run / "task.json")["active_floorplan_freeze"])
        self.assertEqual(sha256(self.run / frozen["path"]), frozen["sha256"])
        next_freeze = floorplan_freeze(self.run, "reviewed changed placement")
        self.assertNotEqual(next_freeze["path"], frozen["path"])

    def test_before_verification_reexports_and_blocks_changed_floorplan(self):
        self.freeze()
        transition(self.run, "ROUTED")
        transition(self.run, "DRC_DONE")
        self.floorplan["instances"][0]["orientation"] = "MX"
        with self.assertRaisesRegex(ValueError, "Manual placement changed"):
            require_unchanged_floorplan(self.run, "verification")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_live_change_between_review_and_freeze_does_not_freeze(self):
        self.pending()
        floorplan_export(self.run)
        self.floorplan["instances"][0]["y"] = 0.5
        result = floorplan_freeze(self.run, "reviewed old export")
        self.assertTrue(result["changed"])
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")
        self.assertFalse((self.run / "floorplan_freezes").exists())

    def test_bad_snapshot_timeout_and_tamper_fail_closed(self):
        self.mock_reader.side_effect = TimeoutError("private secret must not enter task")
        with self.assertRaises(TimeoutError):
            floorplan_export(self.run)
        task = read_json(self.run / "task.json")
        self.assertEqual(task["status"], "NEEDS_ATTENTION")
        self.assertIn("reconcile", task["error"]["message"])
        self.assertNotIn("private secret", json.dumps(task))
        with self.assertRaises(ValueError):
            floorplan_export(self.run)

    def test_frozen_receipt_tamper_detected(self):
        frozen = self.freeze()
        write_json(self.run / frozen["path"], {})
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            require_unchanged_floorplan(self.run, "routing")
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")

    def test_export_tamper_is_not_a_reviewed_freeze(self):
        self.pending()
        floorplan_export(self.run)
        write_json(self.run / "floorplan.json", self.floorplan)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            floorplan_freeze(self.run, "reviewed")
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_strict_snapshot_validation(self):
        cfg, _ = layout_context(self.run)
        for field, value in (("x", float("nan")), ("x", '0 include("bad")'),
                             ("x", True), ("orientation", "R45"),
                             ("bbox", [[1, 0], [0, 2]]), ("instance", 'I0\" system()')):
            item = copy.deepcopy(self.floorplan)
            item["instances"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_floorplan(item, cfg)
        for mutate in (lambda item: item.update(units="unknown"),
                       lambda item: item["cellview"].update(cell="other"),
                       lambda item: item.update(skill="system()"),
                       lambda item: item["instances"].append(copy.deepcopy(item["instances"][0]))):
            item = copy.deepcopy(self.floorplan)
            mutate(item)
            with self.assertRaises(ValueError):
                validate_floorplan(item, cfg)

    def test_output_symlink_escape_and_changed_reader_rejected(self):
        outside = self.base / "outside.json"
        outside.write_text("protected", encoding="utf-8")
        try:
            (self.run / "floorplan.json").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable on this host")
        with self.assertRaisesRegex(ValueError, "redirected outside"):
            floorplan_export(self.run)
        self.assertEqual(outside.read_text(encoding="utf-8"), "protected")
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")

    def test_output_escape_rejected_without_symlink_privileges(self):
        # Exercise the actual local_path guard even on Windows hosts where
        # creating a real symlink requires unavailable privileges.
        from pathlib import Path
        original = Path.resolve
        outside = self.base / "protected.json"
        outside.write_text("protected", encoding="utf-8")
        def resolved(path, *args, **kwargs):
            if path == self.run / "floorplan.json":
                return outside
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolved):
            with self.assertRaisesRegex(ValueError, "redirected outside"):
                floorplan_export(self.run)
        self.assertEqual(outside.read_text(encoding="utf-8"), "protected")

    def test_reader_changed_after_prepare_rejected(self):
        (self.root / "private/layout/reader.py").write_text("def read(context): return {'new': True}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            floorplan_export(self.run)
        self.assertEqual(status(self.run), "LAYOUT_PREPARED")

    def test_cli_export_and_freeze(self):
        self.pending()
        for args in (["floorplan-export", str(self.run)],
                     ["floorplan-freeze", str(self.run), "--reason", "human reviewed"]):
            output = io.StringIO()
            with patch.object(sys, "argv", ["analog-agent", "layout", *args]), patch("sys.stdout", output):
                self.assertEqual(cli.main(), 0)
            self.assertIn("status", json.loads(output.getvalue()))
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")


class LayoutAdapterTest(LayoutRunsFixture):
    def setUp(self):
        super().setUp()
        self.verify_source()

    def test_missing_adapter_does_not_fall_back_to_skill(self):
        run = prepare_layout(self.source)
        with self.assertRaisesRegex(ValueError, "raw SKILL fallback"):
            adapter_read(run)

    def test_plugin_allowlist_and_read_only_context(self):
        cfg = read_json(self.layout_config)
        cfg["layout"]["bridge"] = {"reader": "os:system"}
        write_json(self.layout_config, cfg)
        run = prepare_layout(self.source)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            adapter_read(run)
        reader = self.root / "private/layout/reader.py"
        reader.write_text("def read(context):\n    assert context['operation'] == 'read_placement'\n"
                          "    assert set(context) == {'operation', 'target', 'timeout_s', 'adapter_context'}\n"
                          "    return {'observed': context['target']}\n", encoding="utf-8")
        cfg["layout"]["bridge"]["reader"] = "private/layout/reader.py:read"
        write_json(self.layout_config, cfg)
        run = prepare_layout(self.source)
        self.assertEqual(adapter_read(run)["observed"]["cell"], "top")
        # Real local subprocess handoff with fake plugin, no Virtuoso/import.
        self.assertEqual(read_placement(run, 10)["observed"]["view"], "layout")

    def test_adapter_timeout_terminates_reader(self):
        import subprocess
        cfg = read_json(self.layout_config)
        reader = self.root / "private/layout/reader.py"
        reader.write_text("import time\ndef read(context):\n    time.sleep(20)\n    return {}\n", encoding="utf-8")
        cfg["layout"]["bridge"] = {"reader": "private/layout/reader.py:read"}
        write_json(self.layout_config, cfg)
        run = prepare_layout(self.source)
        spawn = subprocess.Popen
        processes = []
        def record_process(*args, **kwargs):
            process = spawn(*args, **kwargs)
            processes.append((args[0], process))
            return process
        with patch("analog_agent.layout_bridge.subprocess.Popen", side_effect=record_process):
            with self.assertRaisesRegex(TimeoutError, "reconcile"):
                read_placement(run, 0.2)
        readers = [process for command, process in processes if "analog_agent.layout_bridge" in command]
        self.assertEqual(len(readers), 1)
        self.assertIsNotNone(readers[0].poll())
        # On Windows taskkill is another local child, captured by the same spy.
        self.assertTrue(all(process.poll() is not None for _, process in processes))
