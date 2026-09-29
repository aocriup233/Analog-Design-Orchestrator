"""Circuit-independent replay of reviewed campaign decisions.

Only candidate selection is project-owned. Evidence accumulation is handled
here; replay never creates runs or changes live campaigns or knowledge files.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .campaign import _policy, _validate_points
from .core import load_config, read_json, sha256
from .plugins import load_function


def _digest(value: dict) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replay(config_path: Path, history_path: Path) -> dict:
    """Replay synthetic or recorded comparisons entirely in memory."""
    cfg = load_config(config_path)
    history = read_json(history_path)
    if history.get("schema") != 1 or not isinstance(history.get("cycles"), list):
        raise ValueError("Replay history needs schema 1 and a cycles array")
    if not history["cycles"]:
        raise ValueError("Replay history needs at least one cycle")
    initial = history.get("initial_knowledge", {})
    if not isinstance(initial, dict):
        raise ValueError("Replay initial_knowledge must be an object")
    campaign_cfg = cfg.get("campaign", {})
    proposer_spec = campaign_cfg.get("proposer")
    root = Path(cfg["_project_root"])
    propose = load_function(proposer_spec, root) if proposer_spec else None
    policy = _policy(cfg)
    if len(history["cycles"]) > policy["max_cycles"]:
        raise ValueError("Replay exceeds campaign.max_cycles")
    knowledge = copy.deepcopy(initial)
    previous = None
    checkpoints = []
    prior_cycles = []
    for index, item in enumerate(history["cycles"], 1):
        if not isinstance(item, dict):
            raise ValueError(f"Replay cycle {index} must be an object")
        comparison = item.get("comparison")
        decision = item.get("decision")
        if not isinstance(comparison, dict) or not isinstance(decision, dict):
            raise ValueError(f"Replay cycle {index} needs comparison and decision objects")
        cycle_id = f"cycle-{index:04d}"
        if comparison.get("cycle") != cycle_id:
            raise ValueError(f"Replay cycle {index} has a mismatched comparison ID")
        source = item.get("source", "manual")
        if source not in {"manual", "proposal"}:
            raise ValueError(f"Replay cycle {index} has an invalid source")
        context = {"project_config": str(Path(config_path).resolve()),
                   "cycle_count": index - 1, "policy": policy,
                   "latest_comparison": copy.deepcopy(previous),
                   "knowledge": copy.deepcopy(knowledge)}
        if source == "proposal":
            if not propose:
                raise ValueError("A proposal-sourced replay cycle needs campaign.proposer")
            proposal = propose(context)
            if not isinstance(proposal, dict):
                raise TypeError("Replay proposer must return a points object")
            validated = _validate_points({"policy": policy, "cycles": prior_cycles}, proposal, cfg)
        else:
            validated = []
        rows = comparison.get("points")
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Replay cycle {index} has no comparison rows")
        if len(rows) > policy["max_points_per_cycle"] or (
                sum(len(cycle["points"]) for cycle in prior_cycles) + len(rows)
                > policy["max_total_points"]):
            raise ValueError(f"Replay cycle {index} exceeds campaign point limits")
        by_id = {row.get("id"): row for row in rows}
        if len(by_id) != len(rows) or any(not isinstance(key, str) for key in by_id):
            raise ValueError(f"Replay cycle {index} has duplicate or invalid point IDs")
        if any(row.get("state") not in {"ANALYZED", "FAILED"} for row in rows):
            raise ValueError(f"Replay cycle {index} contains unfinished points")
        chosen = decision.get("selected")
        rationale = decision.get("rationale")
        if (not isinstance(chosen, list) or not isinstance(rationale, str) or
                not rationale.strip() or any(point not in by_id or
                by_id[point]["state"] != "ANALYZED" for point in chosen)):
            raise ValueError(f"Replay cycle {index} has an invalid recorded decision")
        if source == "proposal":
            proposed = {_digest(point["parameters"]) for point in validated}
            observed = {_digest(row["parameters"]) for row in rows}
            if not observed <= proposed:
                raise ValueError(f"Replay cycle {index} contains points outside its proposal")
        campaign_id = comparison.get("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise ValueError(f"Replay cycle {index} needs a campaign_id")
        event_id = f"{campaign_id}:{cycle_id}:{_digest(comparison)}"
        knowledge.setdefault("events", []).append({
            "event_id": event_id, "campaign_id": campaign_id, "cycle": cycle_id,
            "comparison_sha256": _digest(comparison), "decision": rationale,
            "selected": copy.deepcopy(chosen), "recorded_at": decision.get("at", "offline-replay")})
        knowledge.setdefault("observations", []).extend({
            "event_id": event_id, "point_id": row["id"],
            "parameters": copy.deepcopy(row["parameters"]), "state": row["state"],
            "verdict": row.get("verdict"), "metrics": copy.deepcopy(row.get("metrics", {})),
            "selected": row["id"] in chosen} for row in rows)
        checkpoints.append({"cycle": cycle_id, "source": source,
                            "proposed_point_ids": [point["id"] for point in validated],
                            "observed_point_ids": list(by_id), "selected": chosen,
                            "knowledge_sha256": _digest(knowledge)})
        prior_cycles.append({"points": rows})
        previous = comparison
    return {"schema": 1, "status": "REPLAYED", "cycle_count": len(checkpoints),
            "config_sha256": sha256(Path(config_path)),
            "history_sha256": sha256(Path(history_path)),
            "checkpoints": checkpoints, "final_knowledge": knowledge}
