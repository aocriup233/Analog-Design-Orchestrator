"""Process-isolated role handoff and narrowly scoped cleanup."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .core import create_run, read_json, run_config, update_status


def invoke_role(role: str, run: Path) -> None:
    """Each role runs in a separate Python process; JSON files are its contract."""
    cfg = run_config(run)
    configured = cfg.get("workflow", {}).get("role_commands", {}).get(role)
    if configured is not None:
        if not isinstance(configured, list) or not configured or not all(isinstance(x, str) for x in configured):
            raise ValueError(f"workflow.role_commands.{role} must be a nonempty argument array")
        command = [part.replace("{run}", str(run)) for part in configured]
    else:
        command = [sys.executable, "-m", "analog_agent.cli", "worker", role, str(run)]
    completed = subprocess.run(
        command,
        capture_output=True, text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{role} worker failed (exit {completed.returncode}):\n"
            f"{completed.stderr or completed.stdout}"
        )
    handoffs = {"netlist": ("netlist_result.json",),
                "simulation": (("staging_result.json",) if
                               cfg.get("simulation", {}).get("backend") == "external" else
                               ("submission_result.json", "simulation_result.json")),
                "analysis": ("analysis_result.json",)}[role]
    if not any((run / name).is_file() for name in handoffs):
        raise RuntimeError(f"{role} worker returned success without a handoff: {handoffs}")


def cleanup(run: Path) -> bool:
    run = run.resolve()
    if not (run / "analysis_result.json").is_file():
        raise ValueError("Analysis result must exist before raw cleanup")
    raw = (run / "raw").resolve()
    if not raw.is_relative_to(run) or raw == run:
        raise ValueError("Refusing cleanup outside run directory")
    if not raw.exists():
        return False
    shutil.rmtree(raw)
    update_status(run, "ANALYZED", raw_cleaned=True)
    return True


def run_auto(config_path: Path, overrides: dict[str, str] | None = None) -> list[dict]:
    from .core import load_config
    cfg = load_config(config_path)
    if cfg.get("workflow", {}).get("submission", "manual") != "auto":
        raise ValueError("Set workflow.submission=auto to run unattended")
    reports = []
    for iteration in range(int(cfg.get("workflow", {}).get("max_iterations", 1))):
        run = create_run(config_path, iteration, overrides if iteration == 0 else None)
        invoke_role("netlist", run)
        invoke_role("simulation", run)
        if not (run / "simulation_result.json").is_file():
            reports.append({"run_id": run.name, "status": read_json(run / "task.json")["status"]})
            break
        sim = read_json(run / "simulation_result.json")
        if sim["status"] != "DONE":
            reports.append({"run_id": run.name, "status": "FAILED", "errors": sim["errors"]})
            break
        invoke_role("analysis", run)
        report = read_json(run / "analysis_result.json")
        if cfg.get("workflow", {}).get("return_mode") == "full":
            report = {"analysis": report, "simulation": sim}
        reports.append(report)
        verdict = report.get("status", report.get("analysis", {}).get("status"))
        if cfg.get("workflow", {}).get("cleanup") == "after_success" and verdict == "PASS":
            cleanup(run)
        if verdict == "PASS":
            break
    return reports
