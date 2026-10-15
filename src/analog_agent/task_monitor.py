"""One durable monitor for scheduled jobs and local worker processes.

STALLED is an alert, not a run-state transition. Nothing here submits, retries,
kills processes, cleans artifacts or claims a remote job was cancelled.
"""
import math
import os
import queue
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import local_path, read_json, utc_now, write_json

TERMINAL = {"DONE", "FAILED", "EXITED", "RECONCILED", "RETRYABLE_FAILED", "NEEDS_HUMAN"}


def options(config):
    cfg = config.get("monitor", {})
    if not isinstance(cfg, dict):
        raise ValueError("monitor must be an object")
    result = {"timeout_s": cfg.get("timeout_s", 3600),
              "heartbeat_timeout_s": cfg.get("heartbeat_timeout_s", 120),
              "progress_timeout_s": cfg.get("progress_timeout_s", 600),
              "poll_interval_s": cfg.get("poll_interval_s", 5)}
    for key, value in result.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"monitor.{key} must be finite and positive")
    return result


class TaskMonitor:
    def __init__(self, run, reference, *, clock=time.time):
        self.run = Path(run).resolve()
        self.path = local_path(self.run, reference)
        if self.path.parent.parent != local_path(self.run, "task_monitors") or self.path.name != "handoff.json":
            raise ValueError("Invalid task monitor reference")
        self.clock = clock

    @classmethod
    def start(cls, run, kind, config, *, identity=None, clock=time.time):
        policy = options(config)
        directory = local_path(Path(run), "task_monitors/" + uuid.uuid4().hex)
        directory.mkdir(parents=True, exist_ok=False)
        monitor = cls(run, str(directory / "handoff.json"), clock=clock)
        now = clock()
        write_json(monitor.path, {"schema_version": 1, "kind": kind, "identity": identity or {},
                   "owner": {"pid": os.getpid(), "host": socket.gethostname()},
                   "started_s": now, "progress_s": now, "progress": None,
                   "policy": policy, "state": "RUNNING", "events": [{"state": "RUNNING", "at": utc_now()}]})
        monitor.heartbeat()
        return monitor

    @property
    def reference(self):
        return self.path.relative_to(self.run).as_posix()

    def heartbeat(self):
        # This is a monitor/worker heartbeat, NOT proof of scheduler progress.
        write_json(local_path(self.path.parent, "heartbeat.json"), {"at_s": self.clock(), "at": utc_now()})

    def record(self, state, *, progress=None, identity=None, failure=None):
        value = read_json(self.path)
        if failure is not None:
            value["failure"] = failure
        if identity is not None:
            value["identity"] = identity
        if progress is not None and progress != value["progress"]:
            value.update(progress=progress, progress_s=self.clock())
        if state != value["state"]:
            value["events"].append({"state": state, "at": utc_now()})
        value["state"] = state
        write_json(self.path, value)
        self.heartbeat()

    def inspect(self):
        value = read_json(self.path)
        if value["state"] in TERMINAL:
            return {**value, "status": value["state"], "alerts": []}
        now, policy = self.clock(), value["policy"]
        alerts = []
        heartbeat = local_path(self.path.parent, "heartbeat.json")
        if not heartbeat.is_file() or now - read_json(heartbeat)["at_s"] > policy["heartbeat_timeout_s"]:
            alerts.append("worker heartbeat missing or stale")
        if now - value["started_s"] > policy["timeout_s"]:
            alerts.append("task timeout exceeded; reconcile before any retry")
        if now - value["progress_s"] > policy["progress_timeout_s"]:
            alerts.append("no observed task progress")
        if value["state"] == "STALLED":
            alerts.append("previous operation failed or timed out; reconcile unknown execution state")
        return {**value, "status": "STALLED" if alerts else value["state"], "alerts": alerts}

    def call(self, function, *, timeout_s=None):
        """Bound a trusted adapter call; timeout never retries the live callable.

        Python cannot terminate an arbitrary adapter thread safely. After timeout
        it may still be executing: retain intent and require reconciliation. The
        daemon only returns an isolated result; it cannot publish a late handoff.
        """
        result = queue.Queue(maxsize=1)
        def execute():
            try:
                result.put((True, function()))
            except BaseException as exc:
                result.put((False, exc))
        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        policy = read_json(self.path)["policy"]
        limit = policy["timeout_s"] if timeout_s is None else min(policy["timeout_s"], timeout_s)
        deadline = time.monotonic() + limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = TimeoutError("Task operation timed out; inspect monitor and reconcile; never resubmit automatically")
                from .failures import classify
                self.record("STALLED", failure=classify(error))
                raise error
            try:
                ok, value = result.get(timeout=min(policy["poll_interval_s"], remaining))
                if not ok:
                    from .failures import classify
                    self.record("STALLED", failure=classify(value))
                    raise value
                return value
            except queue.Empty:
                self.heartbeat()

    def wait_process(self, process, *, timeout_s, progress_paths=()):
        """Periodic local PID polling; caller retains its existing kill policy."""
        self.record("RUNNING", identity={"pid": process.pid, "host": socket.gethostname()})
        deadline = time.monotonic() + timeout_s
        policy = read_json(self.path)["policy"]
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.record("STALLED")
                raise TimeoutError("Local process timeout; reconcile before retry")
            progress = [p.stat().st_size for p in progress_paths if p.is_file()]
            self.record("RUNNING", progress=progress)
            time.sleep(min(policy["poll_interval_s"], remaining))
        self.record("DONE" if process.returncode == 0 else "FAILED", progress=process.returncode)

    @contextmanager
    def watch_process(self, process, *, progress_paths=()):
        """Observe a process while its existing communicate/kill policy executes."""
        stop = threading.Event()
        self.record("RUNNING", identity={"pid": process.pid, "host": socket.gethostname()})
        interval = read_json(self.path)["policy"]["poll_interval_s"]
        def observe():
            while not stop.wait(interval):
                progress = [p.stat().st_size for p in progress_paths if p.is_file()]
                self.record("RUNNING", progress=progress)
        watcher = threading.Thread(target=observe, daemon=True)
        watcher.start()
        failed = False
        try:
            yield
        except BaseException as error:
            from .failures import classify
            self.record("STALLED", failure=classify(error))
            failed = True
            raise
        finally:
            stop.set()
            watcher.join()
            code = process.poll()
            self.record("STALLED" if failed or code is None else "DONE" if code == 0 else "FAILED", progress=code)


def inspect_tasks(run):
    """Read-only inspection: wall-clock expiry is visible without a polling tick."""
    directory = local_path(Path(run), "task_monitors")
    return [{"reference": p.relative_to(run).as_posix(), **TaskMonitor(run, str(p)).inspect()}
            for p in sorted(directory.glob("*/handoff.json"))] if directory.is_dir() else []


def watch_tasks(run, *, max_seconds=60):
    """Bounded polling of already running jobs; never enter submit/stage."""
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be finite and positive")
    run = Path(run).resolve()
    from .core import run_config
    from .simulation_agent import poll
    task = read_json(local_path(run, "task.json"))
    config = run_config(run) if task.get("kind") != "layout" else {}
    interval = options(config)["poll_interval_s"]
    deadline = time.monotonic() + max_seconds
    while True:
        state = read_json(local_path(run, "task.json"))["status"]
        if state in {"SUBMITTED", "RUN"} and config.get("simulation", {}).get("backend") == "lsf":
            poll(run, timeout_s=max(.001, deadline - time.monotonic()))
            state = read_json(local_path(run, "task.json"))["status"]
        tasks = inspect_tasks(run)
        stalled = any(t["status"] == "STALLED" for t in tasks)
        remaining = deadline - time.monotonic()
        report = {"run": str(run), "run_status": state, "status": "STALLED" if stalled else state, "tasks": tasks}
        if stalled or remaining <= 0 or not any(t["state"] not in TERMINAL for t in tasks):
            return report
        time.sleep(min(interval, remaining, 60))
