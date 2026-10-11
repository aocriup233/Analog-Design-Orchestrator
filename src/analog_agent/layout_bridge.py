"""Isolated read-only adapter with private diagnostics and exclusive file channel."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

from .core import _fsync_directory, local_path, read_json, utc_now
from .layout_runs import LayoutEvidenceError, layout_context, layout_path
from .parameters import number_value
from .plugins import load_function


class ReaderInputError(ValueError):
    """Input rejection, not unknown Bridge state."""


class ReaderStateError(RuntimeError):
    """Unknown Bridge state; reconcile before proceeding."""


def adapter_read(run: Path) -> dict:
    cfg, project = layout_context(run)
    reference = cfg.get("bridge", {}).get("reader")
    if not reference:
        raise ReaderInputError("Complete typed read_placement adapter is not configured; raw SKILL fallback is forbidden")
    function = load_function(reference, Path(project["_project_root"]), project)
    context = {"operation": "read_placement", "target": {"library": cfg["library"],
               "cell": cfg["cell"], "view": "layout"},
               "timeout_s": number_value(str(cfg.get("timeout_s", 300)))}
    return function(context)


def _private_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _kill_reader(process) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _diagnosed(error: Exception, relative: str):
    error.diagnostic_log = relative
    return error


def read_placement(run: Path, timeout_s: float) -> dict:
    run = layout_path(run)
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    token = uuid.uuid4().hex
    relative = f"private_logs/reader-{utc_now().replace(':', '-')}-{token}.log"
    log = local_path(run, relative)
    result = local_path(run, f"reader_results/{token}.json")
    log.parent.mkdir(exist_ok=True, mode=0o700)
    result.parent.mkdir(exist_ok=True, mode=0o700)
    if result.exists():
        raise FileExistsError(result)
    with tempfile.TemporaryDirectory(prefix="ado-reader-") as empty_cwd, _private_file(log) as diagnostics:
        command = [sys.executable]
        if sys.version_info >= (3, 11):
            command.append("-P")
        command.extend(["-m", "analog_agent.layout_bridge", str(run), str(result)])
        env = os.environ.copy()
        # Pin the already imported ADO package, not an inherited project search path.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        process = subprocess.Popen(command, cwd=empty_cwd, env=env,
                                   stdout=subprocess.DEVNULL, stderr=diagnostics, text=True, **options)
        try:
            process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            try:
                _kill_reader(process)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            raise _diagnosed(TimeoutError(f"Placement reader timed out; reconcile Bridge; diagnostics: {relative}"), relative) from exc
        finally:
            diagnostics.flush()
            os.fsync(diagnostics.fileno())
    _fsync_directory(log.parent)
    if not result.is_file():
        raise _diagnosed(ReaderStateError(f"Reader exited without a result; diagnostics: {relative}"), relative)
    try:
        envelope = read_json(local_path(run, str(result)))
        if not isinstance(envelope, dict) or envelope.get("status") not in {"ok", "input_error", "evidence_error", "crash"}:
            raise ValueError("Invalid result envelope")
    except (OSError, ValueError) as exc:
        raise _diagnosed(ReaderStateError(f"Invalid reader result; diagnostics: {relative}"), relative) from exc
    if envelope["status"] == "input_error":
        message = "Placement adapter is not implemented" if envelope.get("code") == "not_implemented" else "Placement adapter rejected its input"
        raise _diagnosed(ReaderInputError(f"{message}; diagnostics: {relative}"), relative)
    if envelope["status"] == "evidence_error":
        raise _diagnosed(LayoutEvidenceError(f"Reader input evidence changed; diagnostics: {relative}"), relative)
    if process.returncode or envelope["status"] != "ok":
        raise _diagnosed(ReaderStateError(f"Reader crash; reconcile Bridge; diagnostics: {relative}"), relative)
    if set(envelope) != {"status", "result"}:
        raise _diagnosed(ReaderStateError(f"Invalid result fields; diagnostics: {relative}"), relative)
    return envelope["result"]


def main() -> None:
    if len(sys.argv) != 3:
        raise ValueError("Reader expects a layout run and an exclusive result path")
    run = layout_path(Path(sys.argv[1]))
    result = local_path(run, sys.argv[2])
    if result.parent != local_path(run, "reader_results") or result.suffix != ".json":
        raise ValueError("Result must be a JSON file in this run's reader_results/")
    with _private_file(result) as channel:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                value = adapter_read(run)
            envelope = {"status": "ok", "result": value}
        except LayoutEvidenceError:
            traceback.print_exc(file=sys.stderr)
            envelope = {"status": "evidence_error"}
        except (ValueError, NotImplementedError) as exc:
            traceback.print_exc(file=sys.stderr)
            envelope = {"status": "input_error", "code": "not_implemented" if isinstance(exc, NotImplementedError) else "invalid_input"}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            envelope = {"status": "crash"}
        json.dump(envelope, channel, allow_nan=False)
        channel.flush()
        os.fsync(channel.fileno())
    _fsync_directory(result.parent)


if __name__ == "__main__":
    main()
