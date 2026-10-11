"""Isolated, trusted read-only placement adapter. No raw SKILL dispatch in ADO."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .layout_runs import layout_context
from .parameters import number_value
from .plugins import load_function


def adapter_read(run: Path) -> dict:
    cfg, project = layout_context(run)
    reference = cfg.get("bridge", {}).get("reader")
    if not reference:
        raise ValueError("Complete typed read_placement adapter is not configured; raw SKILL fallback is forbidden")
    function = load_function(reference, Path(project["_project_root"]), project)
    # No live client, executable strings, rule content or arbitrary operations.
    context = {"operation": "read_placement", "target": {"library": cfg["library"],
               "cell": cfg["cell"], "view": "layout"},
               "timeout_s": number_value(str(cfg.get("timeout_s", 300)))}
    return function(context)


def read_placement(run: Path, timeout_s: float) -> dict:
    """Bound the adapter process; never infer cancellation of remote operations."""
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    process = subprocess.Popen([sys.executable, "-m", "analog_agent.layout_bridge", str(run)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **options)
    try:
        stdout, _ = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            # PID-specific process tree, argv only; never shell command interpolation.
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
            finally:
                if process.poll() is None:
                    process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate(timeout=10)
        raise TimeoutError("Placement reader timed out; reconcile the Bridge session before retry") from exc
    if process.returncode:
        # Plugin tracebacks may contain private rule/site information, so do not echo stderr.
        raise RuntimeError("Placement reader failed; inspect the trusted adapter/Bridge session before retry")
    try:
        return json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("Placement reader did not return JSON") from exc


def main() -> None:
    if len(sys.argv) != 2:
        raise ValueError("Placement reader expects one layout run")
    # Adapter logging cannot contaminate the JSON handoff channel.
    with contextlib.redirect_stdout(sys.stderr):
        result = adapter_read(Path(sys.argv[1]))
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
