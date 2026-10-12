"""Isolated trusted typed adapters; no arbitrary Bridge/SKILL operation channel."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

from .core import _fsync_directory, local_path, read_json, write_json
from .layout_bridge import _kill_reader, _private_file, ReaderStateError
from .layout_runs import layout_context, layout_path
from .plugins import load_function
from .parameters import number_value

OPERATIONS = frozenset({"read_layout_snapshot", "read_terminals", "append_path", "append_via",
                        "remove_owned_routes", "save_target", "export_verification_snapshot", "verify",
                        "generate_lvs_source", "normalize_lvs_source", "generate_floorplan"})


def validate_operation(operation):
    """Validate before inspecting a run or granting any adapter capability."""
    if operation not in OPERATIONS:
        raise ValueError("Operation is not in the L0 typed allowlist")
    return operation


def configured(run, operation):
    validate_operation(operation)
    cfg, project = layout_context(run)
    if operation == "generate_floorplan":
        if cfg.get("floorplan", {}).get("mode", "manual") != "plugin":
            raise ValueError("Floorplan generation requires plugin mode")
        reference = cfg["floorplan"].get("generator")
    elif operation in {"generate_lvs_source", "normalize_lvs_source"}:
        key = "generator" if operation == "generate_lvs_source" else "normalizer"
        reference = cfg.get("lvs_source", {}).get(key)
    else:
        reference = cfg.get("verification_adapter") if operation == "verify" else cfg.get("bridge", {}).get("operations")
    if not reference:
        raise ValueError("Audited typed adapter is not configured; raw SKILL/shell fallback is forbidden")
    return cfg, project, reference


def call_adapter(run, request):
    cfg, project, reference = configured(run, request["operation"])
    function = load_function(reference, run.parent.parent, project)
    return function(request["payload"]) if request["operation"] == "generate_floorplan" else function(request)


def execute(run, operation, payload):
    """Bound subprocess lifetime; retain exclusive result/private diagnostics."""
    run = layout_path(run)
    cfg, _, _ = configured(run, operation)  # Reject missing adapters before spawning.
    timeout = number_value(str(cfg.get("timeout_s", 300)))
    token = uuid.uuid4().hex
    directory = local_path(run, f"execution/{token}")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    request, result, log = directory / "request.json", directory / "result.json", directory / "stderr.log"
    write_json(request, {"operation": operation, "target": {"library": cfg["library"], "cell": cfg["cell"], "view": "layout"},
                         "timeout_s": timeout, "payload": payload})
    command = [sys.executable] + (["-P"] if sys.version_info >= (3, 11) else [])
    command += ["-m", "analog_agent.layout_executor", str(run), str(request), str(result)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    with tempfile.TemporaryDirectory(prefix="ado-layout-") as empty, _private_file(log) as diagnostics:
        process = subprocess.Popen(command, cwd=empty, env=env, stdout=subprocess.DEVNULL, stderr=diagnostics, **options)
        try:
            process.communicate(timeout=timeout)
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
            raise TimeoutError(f"Typed operation timeout; reconcile; diagnostics: {log.relative_to(run).as_posix()}") from exc
        finally:
            diagnostics.flush()
            os.fsync(diagnostics.fileno())
    try:
        envelope = read_json(local_path(run, str(result)))
    except (OSError, ValueError) as exc:
        raise ReaderStateError("Adapter result is absent or malformed; reconcile") from exc
    if process.returncode == 0 and envelope == {"status": "unsupported"}:
        raise ValueError("Typed adapter operation is not implemented; no raw fallback")
    if process.returncode or not isinstance(envelope, dict) or envelope.get("status") != "ok" or set(envelope) != {"status", "result"}:
        raise ReaderStateError(f"Typed adapter failed; reconcile; diagnostics: {log.relative_to(run).as_posix()}")
    return envelope["result"]


def main():
    run = layout_path(Path(sys.argv[1]))
    request, result = (local_path(run, arg) for arg in sys.argv[2:4])
    if request.parent != result.parent or request.parent.parent != local_path(run, "execution") or result.name != "result.json":
        raise ValueError("Exclusive result must be in this run's execution request directory")
    with _private_file(result) as channel:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                value = call_adapter(run, read_json(request))
            envelope = {"status": "ok", "result": value}
        except NotImplementedError:
            traceback.print_exc(file=sys.stderr)
            envelope = {"status": "unsupported"}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            envelope = {"status": "crash"}
        json.dump(envelope, channel, allow_nan=False)
        channel.flush()
        os.fsync(channel.fileno())
    _fsync_directory(result.parent)


if __name__ == "__main__":
    main()
