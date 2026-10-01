"""Process-isolated role handoff and narrowly scoped cleanup."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .core import create_run, read_json, run_config, update_status, utc_now, write_json


def role_obligation(run: Path) -> dict:
    """Return the logical role that still owns a run after any process exits.

    This is derived from durable artifacts, not from whether a worker process
    or an SSH session is currently alive.  In particular, staging an external
    simulation is never a completed simulation-role handoff.
    """
    run = run.resolve()
    state = read_json(run / "task.json")["status"]
    backend = run_config(run).get("simulation", {}).get("backend", "bridge")
    actions = {
        "CREATED": ("netlist", "prepare_netlist"),
        "NETLIST_READY": ("simulation", "stage_and_submit"),
        "STAGED": ("simulation", "reconcile_before_submit" if backend == "external"
                   else "submit"),
        "SUBMITTED": ("simulation", "poll"),
        "RUN": ("simulation", "poll"),
        "DONE": ("simulation", "retrieve"),
        "RETRIEVED": ("simulation", "verify"),
        "VERIFIED": ("analysis", "analyze_or_review"),
        "ANALYZED": ("analysis", "review_result"),
        "FAILED": ("analysis", "triage_failure"),
    }
    if state not in actions:
        raise ValueError(f"Unknown run state: {state}")
    role, action = actions[state]
    decision_path = run / "role_decision.json"
    if state in {"VERIFIED", "ANALYZED", "FAILED"} and decision_path.is_file():
        decision = read_json(decision_path)
        if decision["source_status"] != state:
            raise ValueError("Role decision does not match the reviewed run state")
        if decision["decision"] == "accept":
            role, action = None, None
        else:
            role, action = decision["next_role"], "create_successor_run"
    return {"run": str(run), "status": state, "owner_role": role,
            "next_action": action, "role_complete": role is None}


def decide_run(run: Path, decision: str, reason: str,
               next_role: str | None = None) -> dict:
    """Record an explicit analysis/user handoff after verification or failure.

    Never mutates a frozen design or reruns a failed job.  A subsequent role
    starts a new run with its own immutable input snapshot.
    """
    run = run.resolve()
    state = read_json(run / "task.json")["status"]
    if state not in {"VERIFIED", "ANALYZED", "FAILED"}:
        raise ValueError("A run must be verified, analyzed, or failed before a decision")
    if decision not in {"accept", "iterate"} or not reason.strip():
        raise ValueError("Decision must be accept/iterate with a nonempty reason")
    if decision == "accept" and (state != "ANALYZED" or next_role is not None):
        raise ValueError("Only an analyzed run may be accepted")
    if decision == "iterate" and next_role not in {"netlist", "simulation"}:
        raise ValueError("Iteration requires a netlist or simulation owner")
    path = run / "role_decision.json"
    if path.exists():
        raise FileExistsError("Role decision is immutable; review the existing handoff")
    write_json(path, {"decision": decision, "reason": reason.strip(),
                      "source_status": state, "next_role": next_role,
                      "recorded_at": utc_now()})
    return role_obligation(run)


def invoke_role(role: str, run: Path) -> dict:
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
    return role_obligation(run)


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
            reports.append({"run_id": run.name, "status": read_json(run / "task.json")["status"],
                            "obligation": role_obligation(run)})
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
