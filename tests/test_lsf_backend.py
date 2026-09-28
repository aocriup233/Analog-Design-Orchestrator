from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analog_agent.analysis_agent import analyze
from analog_agent.core import create_run, read_json, write_json
from analog_agent.netlist_agent import prepare
from analog_agent.simulation_agent import cleanup_remote, resume, simulate, submit
from analog_agent.state import status, transition


ADAPTER = '''
from pathlib import Path
from analog_agent.core import sha256

class Adapter:
    def stage(self, run, config, plan):
        return {"input_sha256": plan["deck_sha256"], "remote_dir": "/fake/" + run.name}

    def submit(self, run, config, staged):
        marker = run / "submit_count"
        marker.write_text(str(int(marker.read_text()) + 1 if marker.exists() else 1))
        return {"state": "SUBMITTED", "job_id": "42", "remote_dir": staged["remote_dir"]}

    def poll(self, run, config, submitted):
        marker = run / "poll_count"
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        return {"state": "RUN" if count == 1 else "DONE", "job_id": submitted["job_id"]}

    def retrieve(self, run, config, submitted):
        path = run / "raw" / "ac.ac"
        path.parent.mkdir(exist_ok=True)
        path.write_text("fake psf")
        digest = sha256(path)
        return {"artifacts": [{"path": str(path), "sha256": digest,
                                "remote_sha256": digest}]}

    def verify(self, run, config, submitted, retrieved):
        return {"ok": True, "data": {"dc_A": 1.0}, "metadata": {
            "job_id": submitted["job_id"], "remote_dir": submitted["remote_dir"]}}

    def cleanup(self, run, config, submitted):
        return {"status": "DONE", "removed_staging": True}

def create(config, run):
    return Adapter()
'''


class LSFBackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("simulator lang=spectre\n", encoding="utf-8")
        (self.root / "adapter.py").write_text(ADAPTER, encoding="utf-8")
        self.config = self.root / "project.json"
        write_json(self.config, {
            "template": "template.scs", "parameters": {},
            "simulation": {"backend": "lsf", "adapter": "adapter.py:create",
                           "output_format": "psfascii"},
            "metrics": {"dc": {"kind": "scalar", "key": "dc_A"}},
            "rules": {"dc": {"min": 0.5}},
            "workflow": {"submission": "manual", "cleanup": "never"},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_async_lsf_state_survives_separate_resume_calls(self):
        run = create_run(self.config, 0)
        prepare(run)
        self.assertEqual(simulate(run)["status"], "SUBMITTED")
        self.assertEqual(status(run), "SUBMITTED")
        self.assertEqual(resume(run)["status"], "RUN")
        self.assertEqual(status(run), "RUN")
        result = resume(run)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(status(run), "VERIFIED")
        self.assertEqual(result["metadata"]["job_id"], "42")
        self.assertEqual(cleanup_remote(run)["status"], "DONE")
        self.assertEqual(analyze(run)["status"], "PASS")
        self.assertEqual(status(run), "ANALYZED")
        self.assertEqual([event["status"] for event in read_json(run / "task.json")["history"]],
                         ["CREATED", "NETLIST_READY", "STAGED", "SUBMITTED", "RUN",
                          "DONE", "RETRIEVED", "VERIFIED", "ANALYZED"])

    def test_invalid_transition_rejected(self):
        run = create_run(self.config, 0)
        with self.assertRaisesRegex(ValueError, "Invalid run transition"):
            transition(run, "DONE")
        with self.assertRaisesRegex(ValueError, "VERIFIED"):
            cleanup_remote(run)

    def test_persisted_submission_is_adopted_without_duplicate_job(self):
        run = create_run(self.config, 0)
        prepare(run)
        simulate(run)
        task = read_json(run / "task.json")
        task["status"] = "STAGED"  # emulate interruption after handoff write
        write_json(run / "task.json", task)
        submit(run)
        self.assertEqual(status(run), "SUBMITTED")
        self.assertEqual((run / "submit_count").read_text(), "1")

    def test_cli_resumes_across_processes(self):
        def cli(*args):
            completed = subprocess.run([sys.executable, "-m", "analog_agent.cli", *map(str, args)],
                                       text=True, capture_output=True, check=True)
            return json.loads(completed.stdout)

        created = cli("new", self.config)
        run = Path(created["run"])
        self.assertEqual(cli("submit", run)["status"], "SUBMITTED")
        self.assertEqual(cli("resume", run)["status"], "RUN")
        self.assertEqual(cli("resume", run)["status"], "DONE")
        self.assertEqual(cli("analyze", run)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
