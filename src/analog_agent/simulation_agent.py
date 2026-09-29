"""Resumable simulation worker for synchronous and scheduled backends."""

from __future__ import annotations

from pathlib import Path

from .backends import backend_for, json_safe
from .core import read_json, run_config, sha256, utc_now, write_json
from .sim_rules import build_deck
from .state import status, transition


def _config_and_netlist(run: Path):
    cfg = run_config(run)
    net = read_json(run / "netlist_result.json")
    design = Path(net["netlist"]).resolve()
    if not design.is_relative_to(run.resolve()) or sha256(design) != net["netlist_sha256"]:
        raise ValueError("Netlist handoff changed after preparation")
    return cfg, net, design


def stage(run: Path, simulator_factory=None) -> dict:
    run = run.resolve()
    if status(run) == "STAGED":
        return read_json(run / "staging_result.json")
    if status(run) != "NETLIST_READY":
        raise ValueError(f"Cannot stage from {status(run)}")
    cfg, net, design = _config_and_netlist(run)
    existing = run / "staging_result.json"
    if existing.is_file():
        staged = read_json(existing)
        if staged["netlist_sha256"] != net["netlist_sha256"]:
            raise ValueError("Persisted staging handoff has a different netlist hash")
        transition(run, "STAGED")
        return staged
    plan_path = run / "simulation_plan.json"
    if plan_path.is_file():
        plan = read_json(plan_path)
        if plan["design_sha256"] != net["netlist_sha256"] or sha256(Path(plan["deck"])) != plan["deck_sha256"]:
            raise ValueError("Existing simulation plan changed after preparation")
        for item in plan.get("rules", []):
            if sha256(Path(item["path"])) != item["sha256"]:
                raise ValueError("Simulation rule changed after plan creation")
    else:
        rules = cfg.get("simulation", {})
        plan = build_deck(design, cfg, run) if (rules.get("rules") or rules.get("rules_by_testbench")) else {
            "deck": str(design), "deck_sha256": net["netlist_sha256"],
            "design_sha256": net["netlist_sha256"], "rules": [], "statements": []}
    backend = backend_for(cfg, run, simulator_factory)
    staged = backend.stage(run, cfg, plan)
    if staged.get("input_sha256") != plan["deck_sha256"]:
        raise ValueError("Backend did not stage the expected deck hash")
    payload = {"status": "STAGED", "at": utc_now(), "backend": cfg.get("simulation", {}).get("backend", "bridge"),
               "netlist_sha256": net["netlist_sha256"], "deck_sha256": plan["deck_sha256"],
               "backend_data": json_safe(staged)}
    write_json(run / "staging_result.json", payload)
    transition(run, "STAGED")
    return payload


def submit(run: Path, simulator_factory=None) -> dict:
    run = run.resolve()
    if status(run) == "NETLIST_READY":
        stage(run, simulator_factory)
    if status(run) != "STAGED":
        raise ValueError(f"Cannot submit from {status(run)}")
    cfg, _, _ = _config_and_netlist(run)
    staged = read_json(run / "staging_result.json")
    existing = run / "submission_result.json"
    if existing.is_file():
        payload = read_json(existing)
        if payload["netlist_sha256"] != staged["netlist_sha256"] or payload["deck_sha256"] != staged["deck_sha256"]:
            raise ValueError("Persisted submission handoff has different input hashes")
        result = payload["backend_data"]
    else:
        intent = run / "submission_intent.json"
        if intent.is_file():
            previous = read_json(intent)
            if previous.get("deck_sha256") != staged["deck_sha256"]:
                raise ValueError("Submission intent has a different deck hash")
            raise ValueError("Submission outcome unknown; inspect the backend by run ID before retrying")
        write_json(intent, {"started_at": utc_now(), "run_id": run.name,
                            "backend": staged["backend"],
                            "deck_sha256": staged["deck_sha256"]})
        backend = backend_for(cfg, run, simulator_factory)
        result = backend.submit(run, cfg, staged["backend_data"])
    return _record_submission(run, staged, result, existing)


def _record_submission(run: Path, staged: dict, result: dict, existing: Path) -> dict:
    state = result.get("state")
    if state not in {"SUBMITTED", "RUN", "DONE", "FAILED"}:
        raise ValueError(f"Backend submitted an invalid state: {state}")
    if existing.is_file():
        payload = read_json(existing)
    else:
        payload = {"status": state, "submitted_at": utc_now(), "backend": staged["backend"],
                   "netlist_sha256": staged["netlist_sha256"], "deck_sha256": staged["deck_sha256"],
                   "backend_data": json_safe(result)}
        write_json(existing, payload)
    if state == "RUN":
        transition(run, "SUBMITTED", job_id=result.get("job_id"))
        transition(run, "RUN")
    elif state == "FAILED":
        transition(run, "FAILED", job_id=result.get("job_id"))
        write_json(run / "simulation_result.json", {
            "status": "FAILED", "finished_at": utc_now(),
            "errors": result.get("result", {}).get("errors", result.get("errors", [])),
            "netlist_sha256": staged["netlist_sha256"]})
    else:
        transition(run, "SUBMITTED", job_id=result.get("job_id"))
        if state == "DONE":
            transition(run, "DONE")
    return payload


def reconcile_submission(run: Path) -> dict:
    """Adopt a remote job found by a private adapter; never submit it again."""
    run = run.resolve()
    if status(run) != "STAGED":
        raise ValueError("Reconciliation requires a STAGED run")
    existing = run / "submission_result.json"
    if existing.is_file():
        return submit(run)
    intent_path = run / "submission_intent.json"
    if not intent_path.is_file():
        raise ValueError("No uncertain submission intent exists")
    cfg, _, _ = _config_and_netlist(run)
    if cfg.get("simulation", {}).get("backend") != "lsf":
        raise ValueError("Automatic reconciliation requires an LSF site adapter")
    staged = read_json(run / "staging_result.json")
    intent = read_json(intent_path)
    if intent.get("deck_sha256") != staged["deck_sha256"] or intent.get("run_id") != run.name:
        raise ValueError("Submission intent does not match this run")
    result = backend_for(cfg, run).reconcile(run, cfg, staged["backend_data"], intent)
    return _record_submission(run, staged, result, existing)


def poll(run: Path) -> dict:
    run = run.resolve()
    current = status(run)
    if current not in {"SUBMITTED", "RUN"}:
        raise ValueError(f"Cannot poll from {current}")
    cfg = run_config(run)
    submitted = read_json(run / "submission_result.json")
    previous = run / "poll_result.json"
    if previous.is_file() and read_json(previous)["status"] in {"DONE", "FAILED"}:
        result = read_json(previous)["backend_data"]
    else:
        result = backend_for(cfg, run).poll(run, cfg, submitted["backend_data"])
    new = result.get("state")
    if new not in {"SUBMITTED", "RUN", "DONE", "FAILED"}:
        raise ValueError(f"Invalid polled state: {new}")
    payload = {"status": new, "polled_at": utc_now(), "job_id": submitted["backend_data"].get("job_id"),
               "backend_data": json_safe(result)}
    write_json(run / "poll_result.json", payload)
    if new != current:
        transition(run, new)
    if new == "FAILED":
        write_json(run / "simulation_result.json", {
            "status": "FAILED", "finished_at": utc_now(), "errors": result.get("errors", []),
            "netlist_sha256": submitted["netlist_sha256"]})
    return payload


def retrieve(run: Path) -> dict:
    run = run.resolve()
    if status(run) == "RETRIEVED":
        return read_json(run / "retrieval_result.json")
    if status(run) != "DONE":
        raise ValueError(f"Cannot retrieve from {status(run)}")
    cfg = run_config(run)
    submitted = read_json(run / "submission_result.json")
    existing = run / "retrieval_result.json"
    if existing.is_file():
        result = read_json(existing)["backend_data"]
        _check_artifacts(run, result)
        transition(run, "RETRIEVED")
        return read_json(existing)
    result = backend_for(cfg, run).retrieve(run, cfg, submitted["backend_data"])
    _check_artifacts(run, result)
    payload = {"status": "RETRIEVED", "retrieved_at": utc_now(), "backend_data": json_safe(result)}
    write_json(run / "retrieval_result.json", payload)
    transition(run, "RETRIEVED")
    return payload


def verify(run: Path) -> dict:
    run = run.resolve()
    if status(run) == "VERIFIED":
        return read_json(run / "simulation_result.json")
    if status(run) != "RETRIEVED":
        raise ValueError(f"Cannot verify from {status(run)}")
    cfg, net, design = _config_and_netlist(run)
    existing = run / "simulation_result.json"
    if existing.is_file():
        result = read_json(existing)
        if result["status"] != "DONE" or result["netlist_sha256"] != net["netlist_sha256"]:
            raise ValueError("Persisted simulation result does not match the run")
        _check_artifacts(run, read_json(run / "retrieval_result.json")["backend_data"])
        transition(run, "VERIFIED")
        return result
    submitted = read_json(run / "submission_result.json")
    if submitted["netlist_sha256"] != net["netlist_sha256"]:
        raise ValueError("Submitted netlist hash no longer matches")
    plan_path = run / "simulation_plan.json"
    if plan_path.is_file():
        plan = read_json(plan_path)
        if (sha256(Path(plan["deck"])) != plan["deck_sha256"] or
                plan["design_sha256"] != sha256(design) or
                submitted["deck_sha256"] != plan["deck_sha256"]):
            raise ValueError("Simulation deck handoff changed")
    retrieved = read_json(run / "retrieval_result.json")
    _check_artifacts(run, retrieved["backend_data"])
    result = backend_for(cfg, run).verify(run, cfg, submitted["backend_data"],
                                          retrieved["backend_data"])
    if result.get("errors"):
        raise ValueError(f"Backend verification returned errors: {result['errors']}")
    payload = {"status": "DONE", "finished_at": utc_now(),
               "bridge_status": result.get("bridge_status", "VERIFIED"),
               "errors": [], "warnings": result.get("warnings", []),
               "metadata": json_safe(result.get("metadata", {})),
               "data": json_safe(result["data"]),
               "netlist_sha256": net["netlist_sha256"],
               "simulation_plan_sha256": submitted["deck_sha256"]}
    write_json(run / "simulation_result.json", payload)
    transition(run, "VERIFIED")
    if cfg.get("simulation", {}).get("remote_cleanup") == "after_verified":
        cleanup_remote(run)
    return payload


def _check_artifacts(run: Path, retrieved: dict) -> None:
    for artifact in retrieved.get("artifacts", []):
        path = Path(artifact["path"]).resolve(strict=True)
        if not path.is_relative_to(run.resolve()) or sha256(path) != artifact["sha256"]:
            raise ValueError(f"Retrieved artifact changed: {path}")


def cleanup_remote(run: Path) -> dict:
    """Clean site staging only after local results are verified and retained."""
    run = run.resolve()
    if status(run) not in {"VERIFIED", "ANALYZED"}:
        raise ValueError("Remote cleanup requires VERIFIED local results")
    existing = run / "remote_cleanup.json"
    if existing.is_file() and read_json(existing).get("status") in {"DONE", "NOT_APPLICABLE"}:
        return read_json(existing)
    cfg = run_config(run)
    retrieved = read_json(run / "retrieval_result.json")
    _check_artifacts(run, retrieved["backend_data"])
    submitted = read_json(run / "submission_result.json")
    report = backend_for(cfg, run).cleanup(run, cfg, submitted["backend_data"])
    reported = report.get("status")
    expected = "DONE" if cfg.get("simulation", {}).get("backend") == "lsf" else "NOT_APPLICABLE"
    if reported != expected:
        raise ValueError(f"Backend cleanup did not confirm {expected}: {reported}")
    payload = {"status": reported, "cleaned_at": utc_now(), "backend_data": json_safe(report)}
    write_json(existing, payload)
    return payload


def advance(run: Path, simulator_factory=None) -> dict:
    """Make safe progress once; never wait indefinitely for an LSF job."""
    run = run.resolve()
    current = status(run)
    if run_config(run).get("simulation", {}).get("backend") == "external":
        if current == "NETLIST_READY":
            stage(run, simulator_factory)
        return {"status": status(run), "run": str(run),
                "next_action": "site simulation worker must submit and verify the staged deck"}
    if current == "NETLIST_READY":
        stage(run, simulator_factory)
        current = "STAGED"
    if current == "STAGED":
        submit(run, simulator_factory)
        current = status(run)
    if current in {"SUBMITTED", "RUN"}:
        cfg = run_config(run)
        if cfg.get("simulation", {}).get("backend") == "lsf":
            return {"status": current, "run": str(run)}
        poll(run)
        current = status(run)
    if current == "DONE":
        retrieve(run)
        current = "RETRIEVED"
    if current == "RETRIEVED":
        return verify(run)
    return {"status": current, "run": str(run)}


def resume(run: Path) -> dict:
    """Poll an async job and finish retrieval/verification when DONE."""
    run = run.resolve()
    current = status(run)
    if current in {"SUBMITTED", "RUN"}:
        poll(run)
        current = status(run)
    if current in {"DONE", "RETRIEVED"}:
        return advance(run)
    if current == "VERIFIED":
        return read_json(run / "simulation_result.json")
    return {"status": current, "run": str(run)}


def simulate(run: Path, simulator_factory=None) -> dict:
    """Compatibility entry point: synchronous backends finish in one call."""
    if run_config(run.resolve()).get("simulation", {}).get("backend") == "external":
        return advance(run, simulator_factory)
    if status(run.resolve()) in {"SUBMITTED", "RUN", "DONE", "RETRIEVED", "VERIFIED"}:
        return resume(run)
    return advance(run, simulator_factory)
