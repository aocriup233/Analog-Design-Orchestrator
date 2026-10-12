import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent.core import create_run, read_json, write_json
from analog_agent.netlist_agent import prepare
from analog_agent.simulation_agent import stage, submit, poll
from analog_agent.state import status
from analog_agent.task_monitor import TaskMonitor, inspect_tasks, options, watch_tasks


class TaskMonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.now = 100.0
        self.cfg = {"monitor": {"timeout_s": 50, "heartbeat_timeout_s": 10,
                                 "progress_timeout_s": 20, "poll_interval_s": .01}}
        self.monitor = TaskMonitor.start(self.run, "fake_lsf", self.cfg, clock=lambda: self.now)

    def test_no_heartbeat_and_no_progress_alert_read_only(self):
        before = self.monitor.path.read_bytes()
        self.now += 21
        result = self.monitor.inspect()
        self.assertEqual(result["status"], "STALLED")
        self.assertEqual(len(result["alerts"]), 2)
        self.assertEqual(self.monitor.path.read_bytes(), before)

    def test_heartbeat_is_not_progress(self):
        self.monitor.record("RUN", progress="step1")
        self.now += 21
        self.monitor.heartbeat()
        self.assertEqual(self.monitor.inspect()["alerts"], ["no observed task progress"])
        self.monitor.record("RUN", progress="step2")
        self.assertEqual(self.monitor.inspect()["status"], "RUN")

    def test_state_changes_are_durable_terminal_does_not_stall(self):
        for state in ("SUBMITTED", "RUN", "RUN", "DONE"):
            self.monitor.record(state, progress=state)
        self.now += 1000
        self.assertEqual(self.monitor.inspect()["status"], "DONE")
        self.assertEqual([e["state"] for e in read_json(self.monitor.path)["events"]],
                         ["RUNNING", "SUBMITTED", "RUN", "DONE"])

    def test_hanging_adapter_timeout_never_retries(self):
        cfg = {"monitor": {"timeout_s": .04, "poll_interval_s": .01}}
        monitor = TaskMonitor.start(self.run, "hang", cfg)
        release = threading.Event()
        calls = []
        def hang():
            calls.append(1)
            release.wait(1)
            return {"state": "DONE"}
        try:
            with self.assertRaisesRegex(TimeoutError, "reconcile"):
                monitor.call(hang)
            self.assertEqual(monitor.inspect()["status"], "STALLED")
            self.assertEqual(len(calls), 1)
        finally:
            release.set()

    def test_adapter_dies_records_unknown_execution(self):
        def die():
            raise RuntimeError("fake backend died")
        with self.assertRaises(RuntimeError):
            self.monitor.call(die)
        self.assertEqual(self.monitor.inspect()["status"], "STALLED")

    def test_local_process_exit_and_death(self):
        for code in (0, 3):
            process = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
            monitor = TaskMonitor.start(self.run, "local", self.cfg)
            with monitor.watch_process(process):
                process.communicate(timeout=5)
            self.assertEqual(monitor.inspect()["status"], "DONE" if code == 0 else "FAILED")
            self.assertEqual(read_json(monitor.path)["identity"]["pid"], process.pid)

    def test_invalid_policy_and_escape(self):
        for value in (0, -1, True, float("nan")):
            with self.assertRaises(ValueError):
                options({"monitor": {"timeout_s": value}})
        with self.assertRaises(ValueError):
            TaskMonitor(self.run, "../outside.json")

    def test_missing_heartbeat_stalls(self):
        (self.monitor.path.parent / "heartbeat.json").unlink()
        self.assertEqual(self.monitor.inspect()["status"], "STALLED")

    def test_periodic_watch_only_polls_existing_jobs(self):
        write_json(self.run / "task.json", {"status": "RUN"})
        # Ignore the fake-clock fixture monitor while exercising a real watcher.
        self.monitor.record("DONE")
        active = TaskMonitor.start(self.run, "fake_lsf", self.cfg)
        active.record("RUN", progress="running")
        def complete(*args, **kwargs):
            write_json(self.run / "task.json", {"status": "DONE"})
            active.record("DONE", progress="done")
        config = {**self.cfg, "simulation": {"backend": "lsf"}}
        with patch("analog_agent.core.run_config", return_value=config), \
             patch("analog_agent.simulation_agent.poll", side_effect=complete) as polling, \
             patch("analog_agent.simulation_agent.submit") as submitting:
            self.assertEqual(watch_tasks(self.run, max_seconds=.5)["status"], "DONE")
            polling.assert_called_once()
            submitting.assert_not_called()

    def test_watch_never_submits_staged_or_stalled_tasks(self):
        write_json(self.run / "task.json", {"status": "STAGED"})
        with patch("analog_agent.core.run_config", return_value={}), \
             patch("analog_agent.simulation_agent.submit") as submitting, \
             patch("analog_agent.simulation_agent.poll") as polling:
            self.assertEqual(watch_tasks(self.run, max_seconds=.05)["status"], "STALLED")
            submitting.assert_not_called()
            polling.assert_not_called()

    def test_submission_timeout_keeps_intent_and_prevents_duplicate(self):
        (self.run / "template.scs").write_text("simulator lang=spectre\n")
        config = self.run / "project.json"
        write_json(config, {"template": "template.scs", "parameters": {},
                           "simulation": {"backend": "bridge"},
                           "metrics": {"v": {"kind": "scalar", "key": "v"}}, "rules": {"v": {"min": 0}},
                           "monitor": {"timeout_s": .04, "poll_interval_s": .01}})
        run = create_run(config, 0)
        prepare(run)
        stage(run)
        release = threading.Event()
        class Hung:
            calls = 0
            def submit(self, *args):
                self.calls += 1
                release.wait(1)
                return {"state": "DONE"}
        backend = Hung()
        try:
            with patch("analog_agent.simulation_agent.backend_for", return_value=backend):
                with self.assertRaises(TimeoutError):
                    submit(run)
                self.assertEqual(status(run), "STAGED")
                self.assertTrue((run / "submission_intent.json").is_file())
                self.assertTrue(any(t["status"] == "STALLED" for t in inspect_tasks(run)))
                with self.assertRaisesRegex(ValueError, "outcome unknown"):
                    submit(run)
                self.assertEqual(backend.calls, 1)
                self.assertFalse((run / "submission_result.json").exists())
        finally:
            release.set()
