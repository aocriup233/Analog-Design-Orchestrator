"""ADO-01: CLI reports the persisted submission boundary without writes."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import create_run, read_json, sha256, write_json
from analog_agent.netlist_agent import prepare
from analog_agent.simulation_agent import stage
from analog_agent.state import transition


class CliDurableHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
        config = self.root / "project.json"
        write_json(config, {"template": "template.scs", "parameters": {},
                            "simulation": {"backend": "external"},
                            "metrics": {"v": {"kind": "scalar", "key": "v"}},
                            "rules": {"v": {"min": 0}}})
        self.run = create_run(config, 0)
        prepare(self.run)
        stage(self.run)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): sha256(p)
                for p in self.root.rglob("*") if p.is_file()}

    def call(self, command, *, expected=0, invokes=False):
        before = self.snapshot()
        with patch.object(sys, "argv", ["analog-agent", command, str(self.run)]), \
             patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("analog_agent.cli.invoke_role") as invoke:
            code = cli.main()
        self.assertEqual(code, expected)
        self.assertEqual(self.snapshot(), before)
        if invokes:
            invoke.assert_called_once_with("simulation", self.run)
        else:
            invoke.assert_not_called()
        return json.loads(output.getvalue())

    def receipt(self):
        staging = read_json(self.run / "staging_result.json")
        write_json(self.run / "submission_intent.json", {
            "run_id": self.run.name, "deck_sha256": staging["deck_sha256"]})
        write_json(self.run / "submission_result.json", {
            "status": "SUBMITTED", "backend": "external",
            "deck_sha256": staging["deck_sha256"], "netlist_sha256": staging["netlist_sha256"],
            "backend_data": {"state": "SUBMITTED", "job_id": "fake-42"}})
        transition(self.run, "SUBMITTED")

    def test_external_staged_reports_submit_without_submission_files(self):
        output = self.call("submit")
        self.assertEqual(output["status"], "STAGED")
        self.assertEqual(output["staging"], read_json(self.run / "staging_result.json"))
        self.assertEqual(output["obligation"]["next_action"], "submit")
        self.assertFalse((self.run / "submission_intent.json").exists())
        self.assertFalse((self.run / "submission_result.json").exists())

    def test_original_missing_receipt_path_cli_process_exits_zero(self):
        before = self.snapshot()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.run([sys.executable, "-B", "-m", "analog_agent.cli", "submit", str(self.run)],
                                 env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "STAGED")
        self.assertEqual(self.snapshot(), before)

    def test_verified_receipt_preserves_submitted_running_verified_output(self):
        self.receipt()
        submitted = self.call("resume", invokes=True)
        self.assertEqual(submitted["submission"], read_json(self.run / "submission_result.json"))
        transition(self.run, "RUN")
        self.assertEqual(self.call("resume", invokes=True)["status"], "RUN")
        transition(self.run, "DONE")
        transition(self.run, "RETRIEVED")
        write_json(self.run / "simulation_result.json", {
            "status": "DONE", "data": {"v": 1},
            "netlist_sha256": read_json(self.run / "staging_result.json")["netlist_sha256"]})
        transition(self.run, "VERIFIED")
        verified = self.call("resume")
        self.assertEqual(verified["status"], "DONE")
        self.assertNotIn("data", verified)

    def test_intent_without_receipt_is_unknown_and_names_missing_artifact(self):
        write_json(self.run / "submission_intent.json", {"run_id": self.run.name})
        output = self.call("submit", expected=cli.UNKNOWN_STATE_EXIT)
        self.assertEqual(output["status"], "UNKNOWN_STATE")
        self.assertEqual(output["artifact"], str(self.run / "submission_result.json"))
        self.assertEqual(output["next_action"], "reconcile required")

    def test_tampered_pinned_receipt_is_unknown_with_hash_details(self):
        self.receipt()
        write_json(self.run / "submission_result.json", {"status": "SUBMITTED", "tampered": True})
        output = self.call("resume", expected=cli.UNKNOWN_STATE_EXIT)
        self.assertEqual(output["artifact"], str(self.run / "submission_result.json"))
        self.assertIn("expected=", output["reason"])
        self.assertIn("actual=", output["reason"])

    def test_receipt_without_intent_is_unknown(self):
        self.receipt()
        (self.run / "submission_intent.json").unlink()
        output = self.call("resume", expected=cli.UNKNOWN_STATE_EXIT)
        self.assertEqual(output["artifact"], str(self.run / "submission_intent.json"))

    def test_state_conflict_is_unknown(self):
        staging = read_json(self.run / "staging_result.json")
        write_json(self.run / "submission_intent.json", {"run_id": self.run.name})
        write_json(self.run / "submission_result.json", {
            "status": "SUBMITTED", "deck_sha256": staging["deck_sha256"]})
        output = self.call("submit", expected=cli.UNKNOWN_STATE_EXIT)
        self.assertIn("STAGED", output["reason"])


if __name__ == "__main__":
    unittest.main()
