import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent.core import write_json, read_json
from analog_agent.sim_rules import build_deck
from analog_agent.workflow import cleanup


class DurabilityTest(unittest.TestCase):
    def test_json_sync_before_replace_and_no_temporary_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            events = []
            from analog_agent import core
            replace = core.os.replace
            def tracked_replace(*args):
                events.append("replace")
                replace(*args)
            with patch("analog_agent.core.os.fsync", side_effect=lambda _: events.append("file")), \
                    patch("analog_agent.core._fsync_directory", side_effect=lambda _: events.append("directory")), \
                    patch("analog_agent.core.os.replace", side_effect=tracked_replace):
                write_json(path, {"v": 1})
            self.assertEqual(events, ["file", "directory", "replace", "directory"])
            self.assertEqual(read_json(path), {"v": 1})
            with self.assertRaises(ValueError):
                write_json(path, {"v": float("nan")})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            self.assertEqual(read_json(path), {"v": 1})

    def test_rule_snapshot_read_once_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            (run / "netlist").mkdir(parents=True)
            write_json(run / "task.json", {})
            design = run / "netlist" / "input.scs"
            design.write_text("// design")
            rule = root / "rule.json"
            original = b'{"kind": "dc", "name": "snapshot"}'
            rule.write_bytes(original)
            reads = []
            read_bytes = Path.read_bytes
            def tracked_read(path):
                if path == rule:
                    reads.append(path)
                    content = read_bytes(path)
                    path.write_text('{"kind":"dc","name":"changed"}')
                    return content
                return read_bytes(path)
            with patch.object(Path, "read_bytes", tracked_read):
                plan = build_deck(design, {"_project_root": str(root), "simulation": {"rules": ["rule.json"]}}, run)
            self.assertEqual(reads, [rule])
            self.assertEqual(plan["rules"][0]["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(plan["statements"], ["snapshot dc"])

    def test_cleanup_does_not_advance_non_analyzed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "raw").mkdir()
            write_json(run / "task.json", {"status": "VERIFIED"})
            write_json(run / "analysis_result.json", {"status": "PASS"})
            with self.assertRaisesRegex(ValueError, "ANALYZED"):
                cleanup(run)
            self.assertTrue((run / "raw").is_dir())
