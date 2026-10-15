"""S3 failure injection: no simulator, scheduler, Bridge or AI service."""
import errno
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import create_run, read_json, sha256, write_json
from analog_agent.failures import (RetryableError, UnknownStateError, classify, execute_operation,
                                   inspect_attempts, new_attempt, options, record_rollback)
from analog_agent.netlist_agent import prepare
from analog_agent.simulation_agent import stage, submit, retrieve, resume, reconcile_submission
from analog_agent.state import status


class FaultRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("simulator lang=spectre\n")
        (self.root / "adapter.py").write_text("def create(config, run): return None\n")
        self.cfg = {"template": "template.scs", "parameters": {},
                    "simulation": {"backend": "lsf", "adapter": "adapter.py:create"},
                    "metrics": {"v": {"kind": "scalar", "key": "v"}}, "rules": {"v": {"min": 0}},
                    "retry": {"max_attempts": 3, "base_s": .001}}
        write_json(self.root / "project.json", self.cfg)
        self.run = create_run(self.root / "project.json", 0)
        prepare(self.run)
        class Backend:
            def stage(_, run, cfg, plan): return {"input_sha256": plan["deck_sha256"]}
            def submit(_, run, cfg, staged): return {"state": "SUBMITTED", "job_id": "42"}
            def poll(_, run, cfg, submitted): return {"state": "DONE", "job_id": "42"}
            def reconcile(_, run, cfg, staged, intent): return {"state": "SUBMITTED", "job_id": "42"}
            def retrieve(_, run, cfg, submitted):
                path = Path(cfg["_attempt_dir"]) / "result.psf"
                path.write_text("complete")
                return {"artifacts": [{"path": str(path), "sha256": sha256(path), "remote_sha256": sha256(path)}]}
            def verify(_, run, cfg, submitted, retrieved): return {"data": {"v": 1}, "ok": True}
        self.backend = Backend()
        self.patcher = patch("analog_agent.simulation_agent.backend_for", return_value=self.backend)
        self.patcher.start()
        stage(self.run)

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def failures(self):
        return [e["failure"] for e in inspect_attempts(self.run) if "failure" in e]

    def test_exact_classification(self):
        for error, expected in ((ValueError("rules"), "needs_human"),
                                (PermissionError(), "needs_human"),
                                (TimeoutError(), "unknown_state"),
                                (KeyboardInterrupt(), "unknown_state"),
                                (RuntimeError(), "unknown_state"),
                                (RetryableError("license_unavailable", no_side_effects=True), "retryable"),
                                (RetryableError("scheduler_busy"), "unknown_state")):
            self.assertEqual(classify(error)["class"], expected)
        self.assertEqual(classify(ConnectionError(), replay_safe=True)["class"], "retryable")
        self.assertEqual(classify(ConnectionError())["class"], "unknown_state")

    def test_license_then_available_separate_attempts(self):
        paths = []
        def available(run, cfg, staged):
            directory = Path(cfg["_attempt_dir"])
            paths.append(directory)
            (directory / "diagnostic").write_text("attempt " + str(len(paths)))
            if len(paths) == 1:
                raise RetryableError("license_unavailable", no_side_effects=True)
            return {"state": "SUBMITTED", "job_id": "42"}
        self.backend.submit = available
        submit(self.run)
        self.assertEqual(status(self.run), "SUBMITTED")
        self.assertEqual(len(set(paths)), 2)
        from analog_agent.task_monitor import inspect_tasks
        self.assertFalse(any(e["status"] == "STALLED" for e in inspect_tasks(self.run)))
        self.assertEqual((paths[0] / "diagnostic").read_text(), "attempt 1")
        self.assertEqual(self.failures()[-1]["class"], "retryable")
        intent = (self.run / "submission_intent.json").read_bytes()
        resume(self.run)
        self.assertEqual(status(self.run), "VERIFIED")
        self.assertEqual((self.run / "submission_intent.json").read_bytes(), intent)

    def test_backoff_and_hard_cap(self):
        sleeps, paths = [], []
        def busy(directory):
            paths.append(directory)
            raise RetryableError("scheduler_busy", no_side_effects=True)
        with self.assertRaises(RetryableError):
            execute_operation(self.run, "fake_query", {"retry": {"max_attempts": 3, "base_s": 2}}, busy, sleep=sleeps.append)
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(status(self.run), "STAGED")

    def test_network_timeout_submission_requires_reconcile(self):
        calls = []
        def uncertain(*args):
            calls.append(1)
            raise TimeoutError("network timed out after request")
        self.backend.submit = uncertain
        with self.assertRaises(TimeoutError): submit(self.run)
        intent = (self.run / "submission_intent.json").read_bytes()
        with self.assertRaises(UnknownStateError): submit(self.run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(status(self.run), "STAGED")
        self.assertTrue(all(e["class"] == "unknown_state" for e in self.failures()))
        reconcile_submission(self.run)
        self.assertEqual(status(self.run), "SUBMITTED")
        self.assertEqual((self.run / "submission_intent.json").read_bytes(), intent)
        self.assertTrue(any(e["status"] == "RECONCILED" for e in inspect_attempts(self.run)))

    def test_crash_between_intent_and_job_id(self):
        calls = []
        def crash(*args):
            calls.append(1)
            raise KeyboardInterrupt()
        self.backend.submit = crash
        with self.assertRaises(KeyboardInterrupt): submit(self.run)
        self.assertTrue((self.run / "submission_intent.json").is_file())
        self.assertFalse((self.run / "submission_result.json").exists())
        with self.assertRaises(UnknownStateError): submit(self.run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(status(self.run), "STAGED")
        self.assertEqual(self.failures()[-1]["class"], "unknown_state")

    def test_missing_job_id_is_unknown_not_validation_retry(self):
        calls = []
        def missing(*args):
            calls.append(1)
            return {"state": "SUBMITTED"}
        self.backend.submit = missing
        with self.assertRaises(UnknownStateError): submit(self.run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(status(self.run), "STAGED")
        self.assertEqual(self.failures()[-1]["class"], "unknown_state")

    def test_finished_network_error_in_query_can_retry(self):
        calls = []
        def query(directory):
            calls.append(directory)
            if len(calls) == 1: raise ConnectionError("scheduler temporarily unreachable")
            return {"state": "RUN"}
        result = execute_operation(self.run, "fake_poll", self.cfg, query, replay_safe=True, sleep=lambda _: None)
        self.assertEqual(result["state"], "RUN")
        self.assertEqual(len(set(calls)), 2)
        self.assertEqual(self.failures()[-1]["class"], "retryable")

    def test_attempt_log_and_snapshot_tampering_rejected(self):
        directory = new_attempt(self.run, "fake_snapshot")
        preserved = directory / "prior/staging_result.json"
        preserved.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "Prior attempt artifact hash mismatch"):
            inspect_attempts(self.run)

    def test_partial_retrieve_never_overwrites_first_attempt(self):
        submit(self.run)
        from analog_agent.simulation_agent import poll
        poll(self.run)
        paths = []
        def partial(run, cfg, submitted):
            path = Path(cfg["_attempt_dir"]) / "data.psf"
            paths.append(path)
            path.write_text("partial" if len(paths) == 1 else "complete")
            if len(paths) == 1: raise ConnectionError("transfer interrupted")
            digest = sha256(path)
            return {"artifacts": [{"path": str(path), "sha256": digest, "remote_sha256": digest}]}
        self.backend.retrieve = partial
        retrieve(self.run)
        self.assertEqual(status(self.run), "RETRIEVED")
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(paths[0].read_text(), "partial")
        self.assertEqual(self.failures()[-1]["class"], "retryable")

    def test_crash_during_atomic_json_preserves_original(self):
        artifact = self.run / "evidence.json"
        write_json(artifact, {"original": True})
        before = artifact.read_bytes()
        from analog_agent.core import os as core_os
        real_replace = core_os.replace
        def fail_replace(src, dst):
            if Path(dst) == artifact: raise OSError(errno.EIO, "interrupted write")
            return real_replace(src, dst)
        with patch("analog_agent.core.os.replace", side_effect=fail_replace):
            with self.assertRaises(OSError):
                execute_operation(self.run, "fake_write", self.cfg, lambda _: write_json(artifact, {"new": True}))
        self.assertEqual(artifact.read_bytes(), before)
        self.assertEqual(status(self.run), "STAGED")
        self.assertEqual(self.failures()[-1]["class"], "unknown_state")
        self.assertFalse(list(self.run.glob("evidence.json.*.tmp")))

    def test_stale_lock_never_reclaimed(self):
        lock = self.run / ".attempt-fake_query.lock"
        write_json(lock, {"pid": 99999999, "host": "previous-host"})
        before = lock.read_bytes()
        with self.assertRaises(UnknownStateError):
            execute_operation(self.run, "fake_query", self.cfg, lambda _: self.fail("must not execute"))
        self.assertEqual(lock.read_bytes(), before)
        self.assertEqual(status(self.run), "STAGED")

    def test_hard_process_death_is_inferred_read_only(self):
        new_attempt(self.run, "fake_submit", operation=True)
        index = (self.run / "attempts.jsonl").read_bytes()
        self.assertTrue(any(e["status"] == "UNKNOWN_STATE" for e in inspect_attempts(self.run)))
        self.assertEqual((self.run / "attempts.jsonl").read_bytes(), index)
        with self.assertRaises(UnknownStateError):
            execute_operation(self.run, "fake_submit", self.cfg, lambda _: self.fail("must not execute"))

    def test_rollback_is_new_evidence_not_overwrite(self):
        artifact = self.run / "staging_result.json"
        before = artifact.read_bytes()
        first = record_rollback(self.run, "fake_rollback", str(artifact), "review previous output")
        index = (self.run / "attempts.jsonl").read_bytes()
        second = record_rollback(self.run, "fake_rollback", str(artifact), "review again")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue((self.run / "attempts.jsonl").read_bytes().startswith(index))
        self.assertEqual(artifact.read_bytes(), before)
        self.assertEqual(status(self.run), "STAGED")

    def test_invalid_policy_and_path_escape(self):
        for cfg in ({"max_attempts": 0}, {"max_attempts": True}, {"base_s": 0}, {"base_s": float("inf")}):
            with self.assertRaises(ValueError): options({"retry": cfg})
        with self.assertRaises(ValueError): new_attempt(self.run, "../outside")

    def test_typed_layout_retry_preserves_operation_boundary(self):
        from analog_agent.layout_executor import execute
        failure = RetryableError("license_unavailable", no_side_effects=True)
        with patch("analog_agent.layout_executor.layout_path", return_value=self.run), \
             patch("analog_agent.layout_executor.configured", return_value=({}, self.cfg, "audited.py:call")), \
             patch("analog_agent.layout_executor._execute", side_effect=[failure, {"ok": True}]) as worker:
            self.assertEqual(execute(self.run, "verify", {})["ok"], True)
            self.assertEqual(worker.call_count, 2)
        events = [e for e in inspect_attempts(self.run) if e["stage"] == "layout_op_verify"]
        self.assertEqual(sum(e["status"] == "RUNNING" for e in events), 2)
        self.assertEqual(self.failures()[-1]["class"], "retryable")

    def test_layout_lock_owner_requires_reconcile(self):
        from analog_agent.layout_runs import layout_lock
        lock = self.run / ".layout.lock"
        write_json(lock, {"pid": 99999999, "host": "previous-host"})
        before = lock.read_bytes()
        with self.assertRaises(UnknownStateError):
            with layout_lock(self.run): self.fail("must not acquire")
        self.assertEqual(lock.read_bytes(), before)
        self.assertEqual(classify(UnknownStateError())["class"], "unknown_state")

    def test_tamper_is_needs_human_and_preserves_state(self):
        before = (self.run / "task.json").read_bytes()
        (self.root / "template.scs").write_text("changed")
        with self.assertRaises(ValueError): submit(self.run)
        self.assertEqual((self.run / "task.json").read_bytes(), before)
        self.assertEqual(self.failures()[-1]["class"], "needs_human")

    def test_recovery_cli_checks_without_submission(self):
        self.backend.submit = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): submit(self.run)
        before = (self.run / "attempts.jsonl").read_bytes()
        with patch.object(sys, "argv", ["analog-agent", "recover", str(self.run)]), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(), 1)
        self.assertTrue(__import__("json").loads(output.getvalue())["reconcile_required"])
        self.assertEqual((self.run / "attempts.jsonl").read_bytes(), before)
