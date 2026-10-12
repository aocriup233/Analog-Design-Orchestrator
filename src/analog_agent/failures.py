"""Conservative failure classes and immutable attempt evidence. No AI calls."""
from contextlib import contextmanager
from pathlib import Path
import errno
import json
import math
import os
import re
import socket
import shutil
import time
import uuid

from .core import local_path, read_json, sha256, utc_now, write_json, _fsync_directory

CLASSES = frozenset({"retryable", "needs_human", "unknown_state"})


class RetryableError(RuntimeError):
    """Audited adapter attests that its call finished with no unsafe side effects."""
    def __init__(self, code, *, no_side_effects=False):
        if code not in {"network", "license_unavailable", "scheduler_busy"}:
            raise ValueError("Unsupported retryable error code")
        self.code, self.no_side_effects = code, no_side_effects is True
        super().__init__(code)


class UnknownStateError(ValueError):
    pass


def options(config):
    value = config.get("retry", {})
    if not isinstance(value, dict):
        raise ValueError("retry must be an object")
    attempts, base = value.get("max_attempts", 3), value.get("base_s", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise ValueError("retry.max_attempts must be an integer from 1 to 10")
    if isinstance(base, bool) or not isinstance(base, (int, float)) or not math.isfinite(base) or not 0 < base <= 60:
        raise ValueError("retry.base_s must be finite, positive and at most 60")
    return {"max_attempts": attempts, "base_s": base}


def classify(error, stage="", *, replay_safe=False):
    """Exactly one class; never infer safe submission from exception wording."""
    saved = getattr(error, "_ado_failure", None)
    if isinstance(saved, dict) and saved.get("class") in CLASSES:
        return saved
    if isinstance(error, RetryableError):
        category = "retryable" if error.no_side_effects or replay_safe else "unknown_state"
        code = error.code
    elif isinstance(error, (UnknownStateError, KeyboardInterrupt, SystemExit, TimeoutError)):
        category, code = "unknown_state", "interrupted_or_uncertain"
    elif isinstance(error, ConnectionError) or (isinstance(error, OSError) and error.errno in
            {errno.ENETDOWN, errno.ENETUNREACH, errno.ECONNRESET, errno.ECONNREFUSED}):
        category, code = ("retryable" if replay_safe else "unknown_state"), "network"
    elif isinstance(error, (ValueError, TypeError, KeyError, FileNotFoundError, PermissionError, NotImplementedError)):
        category, code = "needs_human", "validation_or_configuration"
    else:
        category, code = "unknown_state", "unclassified_execution_failure"
    # Messages may contain credentials/rule text. Only safe codes go to handoffs.
    return {"class": category, "code": code, "exception": type(error).__name__,
            "next_step": "reconcile actual execution before retry" if category == "unknown_state" else
                         "correct/review inputs; preserve this run" if category == "needs_human" else
                         "bounded retry only at a replay-safe boundary"}


def _stage(stage):
    if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", stage):
        raise ValueError("Invalid attempt stage")
    return stage


@contextmanager
def _lock(run, stage, *, wait_s=0):
    path = local_path(run, f".attempt-{_stage(stage)}.lock")
    deadline = time.monotonic() + wait_s
    while True:
        try:
            stream = path.open("x", encoding="utf-8")
            break
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise UnknownStateError("Attempt lock exists; inspect owner and reconcile; never reclaim automatically") from exc
            time.sleep(.01)
    try:
        with stream:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "started_at": utc_now()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(run)
        yield
    finally:
        path.unlink()
        _fsync_directory(run)


def _event(run, directory, name, value):
    """Exclusive immutable event, plus append-only fsynced JSONL index."""
    path = local_path(directory, name + ".json")
    if path.exists():
        raise UnknownStateError("Attempt event already exists; never overwrite evidence")
    if name in {"success", "failure"}:
        value = {**value, "outputs": [{"path": p.relative_to(run).as_posix(), "sha256": sha256(local_path(directory, str(p)))}
                                      for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "started.json"]}
    write_json(path, {"schema_version": 1, "at": utc_now(), **value})
    entry = {"path": path.relative_to(run).as_posix(), "sha256": sha256(path)}
    # One small append; cooperating writers serialize on this exclusive lock.
    with _lock(run, "attempt_log", wait_s=1):
        index = local_path(run, "attempts.jsonl")
        with index.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(run)
    return entry


def new_attempt(run, stage, *, operation=False):
    run = Path(run).resolve()
    directory = local_path(run, f"attempts/{_stage(stage)}/{uuid.uuid4().hex}")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    previous = []
    for name in ("poll_result.json", "staging_result.json", "submission_result.json", "retrieval_result.json",
                 "simulation_result.json", "analysis_result.json"):
        source = local_path(run, name)
        if source.is_file():
            destination = local_path(directory, "prior/" + name)
            destination.parent.mkdir(exist_ok=True)
            with source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            previous.append({"path": destination.relative_to(run).as_posix(), "sha256": sha256(destination)})
    _event(run, directory, "started", {"stage": stage, "status": "RUNNING", "operation": operation, "prior_artifacts": previous})
    return directory


def record_failure(run, stage, error, *, replay_safe=False):
    """Record preflight/worker failure without editing task state or old receipts."""
    directory = new_attempt(run, stage)
    return _event(Path(run).resolve(), directory, "failure",
                  {"stage": stage, "status": "FAILED", "failure": classify(error, stage, replay_safe=replay_safe)})


def inspect_attempts(run):
    """Read-only: interrupted RUNNING attempts require reconciliation."""
    run = Path(run).resolve()
    index = local_path(run, "attempts.jsonl")
    entries = []
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            reference = json.loads(line)
            path = local_path(run, reference["path"])
            if sha256(path) != reference["sha256"]:
                raise ValueError("Attempt evidence hash mismatch: " + reference["path"])
            value = read_json(path)
            for old in value.get("prior_artifacts", []) + value.get("outputs", []):
                if sha256(local_path(run, old["path"])) != old["sha256"]:
                    raise ValueError("Prior attempt artifact hash mismatch: " + old["path"])
            entries.append({**reference, **value})
    completed = {Path(e["path"]).parent.as_posix() for e in entries if e["status"] != "RUNNING"}
    resolved = {p for e in entries for p in e.get("resolved_attempts", [])}
    for entry in entries:
        if entry["status"] == "RUNNING" and Path(entry["path"]).parent.as_posix() not in completed:
            entry["failure"] = classify(UnknownStateError(), entry["stage"])
            entry["status"] = "UNKNOWN_STATE"
        entry["reconciled"] = Path(entry["path"]).parent.as_posix() in resolved
    # A crash between event creation and index append is still uncertain evidence.
    known = {e["path"] for e in entries}
    for path in sorted(local_path(run, "attempts").glob("*/*/started.json")):
        relative = path.relative_to(run).as_posix()
        if relative not in known:
            value = read_json(local_path(run, relative))
            entries.append({**value, "path": relative, "status": "UNKNOWN_STATE",
                            "failure": classify(UnknownStateError(), value["stage"]),
                            "reconciled": path.parent.relative_to(run).as_posix() in resolved})
    return entries


def execute_operation(run, stage, config, function, *, replay_safe=False, sleep=time.sleep):
    """Retry only finished safe failures; each call gets a fresh output directory."""
    run = Path(run).resolve()
    policy = options(config)
    with _lock(run, stage):
        previous = inspect_attempts(run)
        if any(e["stage"] == stage and e.get("operation") and not e.get("reconciled") and
               (e["status"] == "UNKNOWN_STATE" or e.get("failure", {}).get("class") == "unknown_state")
               for e in previous):
            raise UnknownStateError("Interrupted attempt; inspect and reconcile before continuing")
        for number in range(policy["max_attempts"]):
            directory = new_attempt(run, stage, operation=True)
            try:
                result = function(directory)
                _event(run, directory, "success", {"stage": stage, "status": "DONE"})
                return result
            except BaseException as error:
                failure = classify(error, stage, replay_safe=replay_safe)
                error._ado_failure = failure
                _event(run, directory, "failure", {"stage": stage, "status": "FAILED", "operation": True, "failure": failure})
                if failure["class"] != "retryable" or number + 1 == policy["max_attempts"]:
                    raise
                sleep(min(60, policy["base_s"] * 2 ** number))


def reconciled_submission(run, receipt):
    """Called only after existing scheduler reconcile proves the actual job."""
    run = Path(run).resolve()
    path = local_path(run, receipt)
    value = read_json(path)
    if not value.get("backend_data", {}).get("job_id") or value.get("status") not in {"SUBMITTED", "RUN", "DONE", "FAILED"}:
        raise ValueError("Reconciliation requires a verified job receipt")
    uncertain = {Path(e["path"]).parent.as_posix() for e in inspect_attempts(run)
                 if e["stage"] == "simulation_submit" and not e.get("reconciled") and
                 (e["status"] == "UNKNOWN_STATE" or e.get("failure", {}).get("class") == "unknown_state")}
    directory = new_attempt(run, "reconcile")
    return _event(run, directory, "reconciled", {"stage": "reconcile", "status": "RECONCILED",
                  "receipt": path.relative_to(run).as_posix(), "receipt_sha256": sha256(path),
                  "resolved_attempts": sorted(uncertain)})


def record_rollback(run, stage, source, reason):
    """Evidence-only rollback proposal; never replace artifacts or change state."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Rollback requires a review reason")
    run = Path(run).resolve()
    path = local_path(run, source)
    with _lock(run, stage):
        directory = new_attempt(run, stage)
        return _event(run, directory, "rollback", {"stage": stage, "status": "ROLLBACK_REVIEW",
                     "source": path.relative_to(run).as_posix(), "source_sha256": sha256(path), "reason": reason})
