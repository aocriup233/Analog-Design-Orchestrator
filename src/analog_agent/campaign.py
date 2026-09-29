"""Durable, bounded batches of independent runs with human/AI decisions between cycles.

No circuit-specific search policy lives here. A user, an AI controller, or a
project-local proposer supplies explicit points; this module only executes and
compares them. Every run retains the existing three-role artifact contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import (create_run, load_config, local_path, private_manifest, read_json,
                   run_config, sha256, utc_now, write_json)
from .plugins import load_function
from .state import status as run_status
from .workflow import cleanup, invoke_role


TERMINAL = {"ANALYZED", "FAILED"}
RESUMABLE = {"CREATED", "NETLIST_READY", "STAGED", "SUBMITTED", "RUN",
             "DONE", "RETRIEVED", "VERIFIED"}


def _policy(cfg: dict) -> dict:
    source = cfg.get("campaign", {})
    defaults = {"max_parallel": 1, "max_points_per_cycle": 32,
                "max_cycles": 20, "max_total_points": 200, "poll_interval_s": 10}
    values = {key: source.get(key, default) for key, default in defaults.items()}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"campaign.{key} must be a positive integer")
    if values["max_parallel"] > values["max_points_per_cycle"]:
        raise ValueError("campaign.max_parallel exceeds max_points_per_cycle")
    directions = source.get("metric_directions", {})
    if not isinstance(directions, dict) or any(
            name not in cfg["metrics"] or direction not in {"min", "max"}
            for name, direction in directions.items()):
        raise ValueError("campaign.metric_directions must map configured metrics to min/max")
    values["metric_directions"] = directions
    return values


def _declared_inputs(cfg: dict) -> dict[str, str]:
    """Hash known project inputs; adapters can declare additional dependencies."""
    root = Path(cfg["_project_root"])
    names = set()
    if cfg.get("template"):
        names.add(cfg["template"])
    simulation = cfg.get("simulation", {})
    names.update(simulation.get("rules", []))
    names.update(simulation.get("include_files", []))
    for specification in (cfg.get("netlist", {}).get("generator"),
                          simulation.get("adapter"),
                          cfg.get("campaign", {}).get("proposer"),
                          *(definition.get("function") for definition in cfg.get("metrics", {}).values())):
        if isinstance(specification, str) and ":" in specification:
            location = specification.rsplit(":", 1)[0]
            if location.endswith(".py"):
                names.add(location)
    for rule in simulation.get("rules", []):
        definition = read_json(local_path(root, rule))
        specification = definition.get("function")
        if isinstance(specification, str) and ":" in specification:
            location = specification.rsplit(":", 1)[0]
            if location.endswith(".py"):
                names.add(location)
    extra = cfg.get("campaign", {}).get("dependencies", [])
    if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
        raise ValueError("campaign.dependencies must be a list of project-local paths")
    names.update(extra)
    return {name: sha256(local_path(root, name)) for name in sorted(names)}


@contextmanager
def _lock(campaign: Path):
    """An OS lock, released on process death; no stale sentinel to auto-delete."""
    path = campaign / ".lock"
    with path.open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) != b"1":
            handle.seek(0)
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Campaign is already being changed by another process") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _manifest(campaign: Path) -> dict:
    return read_json(campaign.resolve() / "campaign.json")


def _save(campaign: Path, manifest: dict) -> None:
    manifest["updated_at"] = utc_now()
    write_json(campaign / "campaign.json", manifest)


def create_campaign(config_path: Path) -> Path:
    cfg = load_config(config_path)
    root = Path(cfg["_project_root"])
    identifier = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"
    campaign = root / "campaigns" / identifier
    campaign.mkdir(parents=True, exist_ok=False)
    _save(campaign, {"schema": 1, "campaign_id": identifier,
                     "project_config": str(config_path.resolve()),
                     "path_environment": os.name, "created_at": utc_now(),
                     "status": "AWAITING_POINTS", "policy": _policy(cfg), "cycles": []})
    return campaign


def _validate_points(manifest: dict, payload: dict, cfg: dict) -> list[dict]:
    raw = payload.get("points")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Candidate file needs a nonempty points array")
    policy = manifest["policy"]
    if len(raw) > policy["max_points_per_cycle"]:
        raise ValueError("Candidate count exceeds max_points_per_cycle")
    if len(manifest["cycles"]) >= policy["max_cycles"]:
        raise ValueError("Campaign reached max_cycles")
    prior = sum(len(cycle["points"]) for cycle in manifest["cycles"])
    if prior + len(raw) > policy["max_total_points"]:
        raise ValueError("Campaign reached max_total_points")
    definitions = cfg.get("parameters", {})
    # Repeating a point in a later cycle may be intentional after a topology,
    # PDK, or testbench revision. Only deduplicate within this one batch.
    seen = set()
    validated = []
    identifiers = set()
    for index, point in enumerate(raw, 1):
        if not isinstance(point, dict) or not isinstance(point.get("parameters"), dict):
            raise ValueError(f"Point {index} needs a parameters object")
        point_id = point.get("id", f"p{index:03d}")
        if not isinstance(point_id, str) or not point_id or not point_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid point id: {point_id!r}")
        if point_id in identifiers:
            raise ValueError(f"Duplicate point id: {point_id}")
        identifiers.add(point_id)
        unknown = point["parameters"].keys() - definitions.keys()
        if unknown:
            raise ValueError(f"Unknown parameters in {point_id}: {sorted(unknown)}")
        values = {}
        for name, definition in definitions.items():
            value = point["parameters"].get(name, definition.get("initial"))
            if value is None or isinstance(value, (dict, list, bool)):
                raise ValueError(f"Point {point_id} needs a scalar value for {name}")
            value = str(value)
            if "\n" in value or "\r" in value or "@" in value:
                raise ValueError(f"Unsafe value for {name} in {point_id}")
            values[name] = value
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        if fingerprint in seen:
            raise ValueError(f"Duplicate parameter point: {point_id}")
        seen.add(fingerprint)
        rationale = point.get("rationale", "")
        if not isinstance(rationale, str):
            raise ValueError("Point rationale must be text")
        validated.append({"id": point_id, "parameters": values,
                          "rationale": rationale, "fingerprint": fingerprint,
                          "run": None, "error": None})
    return validated


def add_cycle(campaign: Path, points_path: Path, *, decision: str = "",
              selected: list[str] | None = None) -> dict:
    campaign = campaign.resolve()
    points_path = points_path.resolve()
    with _lock(campaign):
        manifest = _manifest(campaign)
        if manifest["status"] not in {"AWAITING_POINTS", "REVIEW"}:
            raise ValueError("A cycle may be added only before the first cycle or after review")
        if os.name != manifest["path_environment"]:
            raise ValueError("Resume the campaign in the same path environment")
        cfg = load_config(Path(manifest["project_config"]))
        cycle_directions = _policy(cfg)["metric_directions"]
        payload = read_json(points_path)
        points = _validate_points(manifest, payload, cfg)
        if manifest["status"] == "REVIEW":
            if not decision.strip():
                raise ValueError("A new cycle needs the previous review decision")
            previous = manifest["cycles"][-1]
            legal = {point["id"] for point in previous["points"]}
            if any(item not in legal for item in (selected or [])):
                raise ValueError("Selected point is not in the previous cycle")
            previous["decision"] = {"selected": selected or [],
                                    "rationale": decision, "at": utc_now()}
        number = len(manifest["cycles"]) + 1
        cycle = {"id": f"cycle-{number:04d}", "status": "ACTIVE",
                 "created_at": utc_now(), "source": str(points_path),
                 "source_sha256": sha256(points_path),
                 "config_sha256": sha256(Path(manifest["project_config"])),
                 "private_manifest": private_manifest(Path(cfg["_project_root"])),
                 "declared_inputs": _declared_inputs(cfg),
                 "metric_directions": cycle_directions,
                 "points": points}
        manifest["cycles"].append(cycle)
        manifest["status"] = "ACTIVE"
        _save(campaign, manifest)
        return {"campaign": str(campaign), "cycle": cycle["id"],
                "point_count": len(points), "status": "ACTIVE"}


def _point_state(point: dict) -> str:
    return run_status(Path(point["run"])) if point["run"] else "QUEUED"


def _cycle_input_issue(manifest: dict, cycle: dict) -> str | None:
    try:
        config_path = Path(manifest["project_config"])
        cfg = load_config(config_path)
        if (sha256(config_path) != cycle["config_sha256"] or
                private_manifest(config_path.parent) != cycle["private_manifest"] or
                _declared_inputs(cfg) != cycle["declared_inputs"]):
            return "Project configuration or declared input changed during this cycle"
    except Exception as exc:
        return f"Cannot verify cycle inputs: {type(exc).__name__}: {exc}"
    return None


def _advance(run: Path) -> str:
    current = run_status(run)
    if current == "CREATED":
        invoke_role("netlist", run)
        current = run_status(run)
    if current in {"NETLIST_READY", "STAGED", "SUBMITTED", "RUN", "DONE", "RETRIEVED"}:
        invoke_role("simulation", run)
        current = run_status(run)
    if current == "VERIFIED":
        invoke_role("analysis", run)
        cfg = run_config(run)
        if (cfg.get("workflow", {}).get("cleanup") == "after_success"
                and read_json(run / "analysis_result.json")["status"] == "PASS"):
            cleanup(run)
        current = run_status(run)
    return current


def _dispatch(points: list[dict], max_workers: int) -> list[tuple[dict, str | None]]:
    results = []
    if not points:
        return results
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {executor.submit(_advance, Path(point["run"])): point for point in points}
        for future in as_completed(pending):
            point = pending[future]
            try:
                results.append((point, future.result()))
            except Exception as exc:
                results.append((point, f"{type(exc).__name__}: {exc}"))
                point["error"] = f"{type(exc).__name__}: {exc}"
    return results


def _dominates(a: dict, b: dict, directions: dict[str, str]) -> bool:
    better_or_equal = all((a[name] <= b[name] if direction == "min" else a[name] >= b[name])
                          for name, direction in directions.items())
    strictly_better = any((a[name] < b[name] if direction == "min" else a[name] > b[name])
                          for name, direction in directions.items())
    return better_or_equal and strictly_better


def _pareto(rows: list[dict], directions: dict) -> list[str]:
    if not directions:
        return []
    usable = [row for row in rows if row.get("metrics") and all(
        name in row["metrics"] and math.isfinite(float(row["metrics"][name]))
        for name in directions)]
    return [row["id"] for row in usable if not any(
        other is not row and _dominates(other["metrics"], row["metrics"], directions)
        for other in usable)]


def _comparison(campaign: Path, manifest: dict, cycle: dict) -> dict:
    rows = []
    for point in cycle["points"]:
        row = {"id": point["id"], "parameters": point["parameters"],
               "rationale": point["rationale"], "run": point["run"],
               "state": _point_state(point)}
        run = Path(point["run"])
        analysis = run / "analysis_result.json"
        simulation = run / "simulation_result.json"
        if analysis.is_file():
            report = read_json(analysis)
            row.update(verdict=report["status"], metrics=report["metrics"],
                       verdicts=report["verdicts"])
        elif simulation.is_file():
            row["simulation"] = {key: value for key, value in read_json(simulation).items()
                                 if key in {"status", "errors"}}
        rows.append(row)
    directions = cycle["metric_directions"]
    feasible = [row for row in rows if row.get("verdict") == "PASS"]
    report = {"campaign_id": manifest["campaign_id"], "cycle": cycle["id"],
              "completed_at": utc_now(), "points": rows,
              "metric_directions": directions,
              "pareto_point_ids": _pareto(rows, directions),
              "feasible_pareto_point_ids": _pareto(feasible, directions),
              "ranking": "not_configured" if not directions else "pareto_only",
              "decision_required": True}
    write_json(campaign / f"{cycle['id']}_comparison.json", report)
    return report


def step(campaign: Path) -> dict:
    """Do one bounded, restartable scheduling tick; never ask an LLM to poll."""
    campaign = campaign.resolve()
    with _lock(campaign):
        manifest = _manifest(campaign)
        if os.name != manifest["path_environment"]:
            raise ValueError("Resume the campaign in the same path environment")
        if manifest["status"] not in {"ACTIVE", "AWAITING_EXECUTION", "NEEDS_ATTENTION"}:
            return inspect_campaign(campaign)
        cycle = manifest["cycles"][-1]
        input_issue = _cycle_input_issue(manifest, cycle)
        try:
            external = (load_config(Path(manifest["project_config"]))
                        .get("simulation", {}).get("backend") == "external")
        except Exception:
            external = False  # input_issue already records the validation failure
        if input_issue:
            cycle["issue"] = input_issue
            cycle["status"] = manifest["status"] = "NEEDS_ATTENTION"
        elif cycle.pop("issue", None) and not any(point["error"] for point in cycle["points"]):
            cycle["status"] = manifest["status"] = "ACTIVE"
        elif manifest["status"] == "AWAITING_EXECUTION":
            cycle["status"] = manifest["status"] = "ACTIVE"
        points = cycle["points"]
        parallel = manifest["policy"]["max_parallel"]
        # First poll/retrieve/analyze existing work. New submissions use slots freed here.
        if not input_issue:
            active = [point for point in points if point["run"] and not point["error"]
                      and _point_state(point) in {"SUBMITTED", "RUN", "DONE", "RETRIEVED", "VERIFIED"}]
            _dispatch(active, parallel)
            _save(campaign, manifest)
        if (manifest["status"] == "ACTIVE" and not cycle.get("issue")
                and not any(point["error"] for point in points)):
            occupied_states = ({"STAGED", "SUBMITTED", "RUN", "DONE", "RETRIEVED"}
                               if external else {"SUBMITTED", "RUN"})
            occupied = sum(_point_state(point) in occupied_states for point in points
                           if point["run"])
            slots = max(0, parallel - occupied)
            candidates = [point for point in points if not point["error"] and
                          _point_state(point) in ({"QUEUED", "CREATED", "NETLIST_READY"}
                                                   if external else
                                                   {"QUEUED", "CREATED", "NETLIST_READY", "STAGED"})]
            selected = candidates[:slots]
            for point in selected:
                if not point["run"]:
                    run = create_run(Path(manifest["project_config"]), 0, point["parameters"])
                    point["run"] = str(run)
                    _save(campaign, manifest)
            _dispatch(selected, parallel)
            _save(campaign, manifest)
        if cycle.get("issue") or any(point["error"] for point in points):
            cycle["status"] = manifest["status"] = "NEEDS_ATTENTION"
        elif all(point["run"] and _point_state(point) in TERMINAL for point in points):
            _comparison(campaign, manifest, cycle)
            cycle["status"] = manifest["status"] = "REVIEW"
        elif external and any(point["run"] and _point_state(point) in
                              {"STAGED", "SUBMITTED", "RUN", "DONE", "RETRIEVED"}
                              for point in points):
            cycle["status"] = manifest["status"] = "AWAITING_EXECUTION"
        _save(campaign, manifest)
        return inspect_campaign(campaign)


def inspect_campaign(campaign: Path) -> dict:
    """Read-only restart report, independent of chat context or token budget."""
    campaign = campaign.resolve()
    manifest = _manifest(campaign)
    cycles = []
    for cycle in manifest["cycles"]:
        points = []
        for point in cycle["points"]:
            state = "QUEUED"
            issue = point.get("error")
            if point["run"]:
                run = Path(point["run"])
                try:
                    state = run_status(run)
                    run_config(run)  # checks private override hashes and current paths
                    net = run / "netlist_result.json"
                    if net.is_file():
                        result = read_json(net)
                        if sha256(Path(result["netlist"])) != result["netlist_sha256"]:
                            issue = "Netlist hash mismatch"
                    plan = run / "simulation_plan.json"
                    if plan.is_file():
                        result = read_json(plan)
                        if sha256(Path(result["deck"])) != result["deck_sha256"]:
                            issue = "Simulation deck hash mismatch"
                    if (state == "STAGED" and (run / "submission_intent.json").is_file()
                            and not (run / "submission_result.json").is_file()):
                        issue = "Submission outcome unknown; inspect scheduler by run ID"
                    if state in {"SUBMITTED", "RUN", "DONE", "RETRIEVED", "VERIFIED", "ANALYZED"}:
                        if not (run / "submission_result.json").is_file():
                            issue = "Submission receipt missing"
                    retrieval = run / "retrieval_result.json"
                    if retrieval.is_file() and not read_json(run / "task.json").get("raw_cleaned"):
                        for artifact in read_json(retrieval).get("backend_data", {}).get("artifacts", []):
                            artifact_path = Path(artifact["path"]).resolve(strict=True)
                            if not artifact_path.is_relative_to(run.resolve()) or sha256(artifact_path) != artifact["sha256"]:
                                issue = "Retrieved artifact hash mismatch"
                    if state in {"VERIFIED", "ANALYZED"} and not (run / "simulation_result.json").is_file():
                        issue = "Verified simulation result missing"
                    if state == "ANALYZED" and not (run / "analysis_result.json").is_file():
                        issue = "Analysis report missing"
                except Exception as exc:
                    issue = f"{type(exc).__name__}: {exc}"
            points.append({"id": point["id"], "run": point["run"], "state": state,
                           "issue": issue, "parameters": point["parameters"]})
        counts = {state: sum(point["state"] == state for point in points)
                  for state in sorted({point["state"] for point in points})}
        current_issue = (_cycle_input_issue(manifest, cycle)
                         if cycle is manifest["cycles"][-1] and manifest["status"] in
                         {"ACTIVE", "AWAITING_EXECUTION", "NEEDS_ATTENTION"} else None)
        cycles.append({"id": cycle["id"], "status": cycle["status"],
                       "issue": current_issue or cycle.get("issue"),
                       "counts": counts, "points": points,
                       "comparison": str(campaign / f"{cycle['id']}_comparison.json")
                       if (campaign / f"{cycle['id']}_comparison.json").is_file() else None})
    return {"campaign": str(campaign), "status": manifest["status"],
            "policy": manifest["policy"], "cycles": cycles,
            "next_action": {"ACTIVE": "step or run", "PAUSED": "resume",
                            "AWAITING_EXECUTION": "site simulation worker verifies staged runs, then step",
                            "NEEDS_ATTENTION": "drain existing jobs, inspect issues, then retry safe points",
                            "REVIEW": "review comparison, then add next cycle or finish",
                            "AWAITING_POINTS": "add points", "CLOSED": "none"}[manifest["status"]]}


def brief(campaign: Path) -> dict:
    """Small AI handoff after context loss; raw arrays and private config stay out."""
    campaign = campaign.resolve()
    manifest = _manifest(campaign)
    health = inspect_campaign(campaign)
    history = [{"cycle": cycle["id"], "decision": cycle["decision"]}
               for cycle in manifest["cycles"] if "decision" in cycle]
    latest = health["cycles"][-1] if health["cycles"] else None
    packet = {"campaign": str(campaign), "status": health["status"],
              "next_action": health["next_action"], "policy": manifest["policy"],
              "decision_history": history, "current_cycle": latest}
    if latest and latest["comparison"]:
        comparison = read_json(Path(latest["comparison"]))
        packet["latest_comparison"] = {
            "cycle": comparison["cycle"], "metric_directions": comparison["metric_directions"],
            "pareto_point_ids": comparison["pareto_point_ids"],
            "feasible_pareto_point_ids": comparison["feasible_pareto_point_ids"],
            "points": [{key: row[key] for key in
                        ("id", "parameters", "state", "verdict", "metrics", "simulation")
                        if key in row} for row in comparison["points"]],
        }
    return packet


def run_until_review(campaign: Path, max_seconds: int = 3600) -> dict:
    if max_seconds < 1:
        raise ValueError("max_seconds must be positive")
    deadline = time.monotonic() + max_seconds
    while True:
        report = step(campaign)
        draining = (report["status"] == "NEEDS_ATTENTION" and
                    not report["cycles"][-1].get("issue") and any(
            point["state"] in {"SUBMITTED", "RUN", "DONE", "RETRIEVED", "VERIFIED"}
            and not point["issue"]
            for cycle in report["cycles"][-1:] for point in cycle["points"]))
        if (report["status"] != "ACTIVE" and not draining) or time.monotonic() >= deadline:
            return report
        waiting_on_job = any(point["state"] in {"SUBMITTED", "RUN"}
                             for point in report["cycles"][-1]["points"])
        if waiting_on_job:
            interval = report["policy"]["poll_interval_s"]
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def pause(campaign: Path) -> dict:
    with _lock(campaign.resolve()):
        manifest = _manifest(campaign)
        if manifest["status"] not in {"ACTIVE", "AWAITING_EXECUTION"}:
            raise ValueError("Only an active or awaiting-execution campaign can be paused")
        manifest["status"] = "PAUSED"
        _save(campaign, manifest)
    return inspect_campaign(campaign)


def resume(campaign: Path) -> dict:
    with _lock(campaign.resolve()):
        manifest = _manifest(campaign)
        if manifest["status"] != "PAUSED":
            raise ValueError("Only a paused campaign can be resumed")
        manifest["status"] = "ACTIVE"
        _save(campaign, manifest)
    return inspect_campaign(campaign)


def retry(campaign: Path, point_id: str) -> dict:
    """Explicitly retry only a state whose next step cannot duplicate submission."""
    with _lock(campaign.resolve()):
        manifest = _manifest(campaign)
        if manifest["status"] != "NEEDS_ATTENTION":
            raise ValueError("Retry is only available after a worker error")
        cycle = manifest["cycles"][-1]
        point = next((item for item in cycle["points"] if item["id"] == point_id), None)
        if point is None or not point["error"]:
            raise ValueError("Point has no recorded worker error")
        run = Path(point["run"]) if point["run"] else None
        state = run_status(run) if run else "QUEUED"
        if (state == "STAGED" and (run / "submission_intent.json").is_file()
                and not (run / "submission_result.json").is_file()):
            raise ValueError("Submission outcome unknown; reconcile the remote job first")
        if state not in RESUMABLE | {"QUEUED"}:
            raise ValueError(f"Cannot retry a point in {state}")
        if run:
            run_config(run)
        point["error"] = None
        manifest["status"] = cycle["status"] = "ACTIVE"
        _save(campaign, manifest)
    return inspect_campaign(campaign)


def finish(campaign: Path, decision: str, selected: list[str] | None = None) -> dict:
    with _lock(campaign.resolve()):
        manifest = _manifest(campaign)
        if manifest["status"] != "REVIEW" or not decision.strip():
            raise ValueError("Finish requires a reviewed cycle and a decision")
        cycle = manifest["cycles"][-1]
        legal = {point["id"] for point in cycle["points"]}
        if any(item not in legal for item in (selected or [])):
            raise ValueError("Selected point is not in the final cycle")
        cycle["decision"] = {"selected": selected or [], "rationale": decision, "at": utc_now()}
        manifest["status"] = "CLOSED"
        _save(campaign, manifest)
    return inspect_campaign(campaign)


def propose(campaign: Path) -> dict:
    """Call an optional user-owned proposer; never auto-submit its suggestion."""
    manifest = _manifest(campaign)
    if manifest["status"] not in {"AWAITING_POINTS", "REVIEW"}:
        raise ValueError("Proposals are accepted only at a decision boundary")
    cfg = load_config(Path(manifest["project_config"]))
    specification = cfg.get("campaign", {}).get("proposer")
    if not specification:
        raise ValueError("Configure campaign.proposer in the user project")
    latest = manifest["cycles"][-1] if manifest["cycles"] else None
    comparison_path = campaign / f"{latest['id']}_comparison.json" if latest else None
    context = {"campaign_id": manifest["campaign_id"],
               "cycle_count": len(manifest["cycles"]), "policy": manifest["policy"],
               "project_config": manifest["project_config"],
               "latest_comparison": read_json(comparison_path) if comparison_path else None}
    result = load_function(specification, Path(cfg["_project_root"]))(context)
    if not isinstance(result, dict):
        raise TypeError("Campaign proposer must return an object with points")
    _validate_points(manifest, result, cfg)
    path = campaign / f"proposal_{uuid.uuid4().hex[:8]}.json"
    write_json(path, result)
    return {"proposal": str(path), "point_count": len(result["points"]),
            "review_required": True}
