"""Generate JSON proposals only; no placement application or Bridge capability."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .core import local_path, read_json, sha256, update_status
from .layout_constraints import _pinned, digest, frozen_constraints
from .layout_executor import execute
from .layout_floorplan import _attention, _publish, instance_identifier, validate_floorplan
from .layout_floorplan_rules import RULES_PATH, validate_rules
from .layout_floorplan_consistency import check_consistency
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .input_gate import checked_stage
from .layout_worker import publish
from .state import status


def _private_input(root, path):
    resolved = local_path(root, path)
    if not resolved.is_relative_to(root / "private"):
        raise ValueError("User placement input must be inside project private/")
    return resolved


def check_generation(run, floorplan, reference):
    try:
        return _check_generation(run, floorplan, reference)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise LayoutEvidenceError("Generated floorplan evidence is missing or changed") from exc


def _check_generation(run, floorplan, reference):
    cfg, _ = layout_context(run)
    proof = _pinned(run, reference)
    if proof.get("candidate") and _pinned(run, proof["candidate"]) != floorplan:
        raise LayoutEvidenceError("Immutable candidate snapshot changed")
    inputs = read_json(local_path(run, "layout_inputs.json"))
    location = cfg["floorplan"]["generator"].rsplit(":", 1)[0]
    path = local_path(run.parent.parent, location).relative_to(run.parent.parent).as_posix()
    plugin_hash = inputs["private_sha256"][path]
    if (proof["candidate_sha256"] != digest(floorplan) or floorplan["plugin_sha256"] != plugin_hash
            or proof["plugin_sha256"] != plugin_hash or proof["rules_sha256"] != inputs["private_sha256"][RULES_PATH]
            or proof["inputs_sha256"] != sha256(local_path(run, "layout_inputs.json"))
            or proof["constraints_freeze"] != read_json(local_path(run, "task.json")).get("active_constraints_freeze")):
        raise LayoutEvidenceError("Generated floorplan provenance does not bind the current frozen inputs")
    if proof["user_placement"]:
        entry = proof["user_placement"]
        if sha256(_private_input(run.parent.parent, entry["path"])) != entry["sha256"]:
            raise LayoutEvidenceError("User placement input changed since generation")
    return proof


def _references(rules, floorplan, constraints, leading):
    names = {i["instance"] for i in floorplan["instances"]}
    groups = {c["id"] for c in constraints["document"]["constraints"] if c["kind"] == "matching"}
    missing = set()
    for key in ("rows", "regions", "spacing", "guard_rings"):
        for rule in rules[key]:
            for name in rule.get("instances", rule.get("members", [])):
                canonical = instance_identifier(name, allow_leading_digit=leading)
                if canonical not in names:
                    missing.add(canonical)
    for rule in rules["matching_patterns"]:
        if rule["group_id"] not in groups:
            missing.add("matching:" + rule["group_id"])
    pins = {p["name"] for p in floorplan["pins"]}
    missing.update("pin:" + r["pin"] for r in rules["pin_sides"] if r["pin"] not in pins)
    if missing:
        raise ValueError("Orphan floorplan rule references: " + ", ".join(sorted(missing)))


@checked_stage("layout_generate")
def floorplan_generate(run: Path, *, placement: Path | None = None):
    run = layout_path(run)
    with layout_lock(run) as session:
        if status(run) != "FLOORPLAN_PENDING":
            raise ValueError("Generate requires FLOORPLAN_PENDING after the human constraints gate")
        try:
            cfg, _ = layout_context(run)
            if cfg.get("floorplan", {}).get("mode", "manual") != "plugin":
                raise ValueError("Floorplan generation requires plugin mode; manual placement is unchanged")
            constraints = frozen_constraints(run, session=session)
            root = run.parent.parent
            inputs = read_json(local_path(run, "layout_inputs.json"))
            location = cfg["floorplan"]["generator"].rsplit(":", 1)[0]
            plugin_path = local_path(root, location).relative_to(root).as_posix()
            plugin_hash = inputs["private_sha256"][plugin_path]
            rule_raw = local_path(root, RULES_PATH).read_bytes()
            rule_hash = hashlib.sha256(rule_raw).hexdigest()
            if rule_hash != inputs["private_sha256"][RULES_PATH]:
                raise LayoutEvidenceError("Floorplan rule bytes changed")
            rules = validate_rules(json.loads(rule_raw), leading=cfg.get("allow_leading_digit", False))
            consistency = check_consistency(rules, constraints["document"])
            user, entry = None, None
            if placement is not None:
                path = _private_input(root, placement)
                raw = path.read_bytes()
                entry = {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()}
                user = validate_floorplan(json.loads(raw), cfg)
                if user["source"] != "manual":
                    raise ValueError("Echo input must be a user-supplied source=manual placement")
            context = {"schema_version": 1, "run_id": run.name,
                       "target": {"library": cfg["library"], "cell": cfg["cell"], "view": "layout"},
                       "inputs_sha256": sha256(local_path(run, "layout_inputs.json")),
                       "schematic": inputs["schematic"], "constraints": constraints["document"],
                       "rules": rules, "rules_sha256": rule_hash, "plugin_sha256": plugin_hash,
                       "user_placement": user, "user_placement_sha256": entry["sha256"] if entry else None,
                       "tech_summary": cfg.get("tech_summary")}
            candidate = validate_floorplan(execute(run, "generate_floorplan", context), cfg)
            if candidate["source"] != "plugin" or candidate["plugin_sha256"] != plugin_hash:
                raise ValueError("Generator must return source=plugin and its pinned plugin_sha256")
            _references(rules, candidate, constraints, cfg.get("allow_leading_digit", False))
            layout_context(run)
            if entry:
                try:
                    unchanged = sha256(_private_input(root, entry["path"])) == entry["sha256"]
                except (ValueError, OSError) as exc:
                    raise LayoutEvidenceError("User placement input disappeared during generation") from exc
                if not unchanged:
                    raise LayoutEvidenceError("User placement input changed during generation")
            candidate_ref = publish(run, "floorplan_candidates", candidate)
            proof = publish(run, "floorplan_generations", {"schema_version": 1,
                "candidate": candidate_ref,
                "consistency": consistency,
                "floorplan": candidate,
                "candidate_sha256": digest(candidate), "plugin_sha256": plugin_hash,
                "rules_sha256": rule_hash, "inputs_sha256": context["inputs_sha256"],
                "constraints_freeze": constraints["reference"], "user_placement": entry})
            reference = {**_publish(run, candidate), "generation": proof}
            update_status(run, status(run), floorplan_export=reference, active_floorplan_generation=proof)
            return {**reference, "status": status(run), "applied": False}
        except Exception as exc:
            _attention(run, exc)
            raise
