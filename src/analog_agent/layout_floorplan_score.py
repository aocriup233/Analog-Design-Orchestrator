"""Deterministic geometry/center-HPWL scoring, not an electrical/routing model."""
from __future__ import annotations

from fractions import Fraction
import math
from pathlib import Path

from .core import local_path, read_json, sha256
from .layout_constraints import frozen_constraints, topology
from .layout_floorplan import _attention, _capture, instance_identifier, placement_sha256, EXPORT_STATES
from .layout_floorplan_diff import reviewed_candidate
from .layout_floorplan_rules import RULES_PATH, validate_rules
from .layout_placement_check import check_placement
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .parameters import number_value


def score_floorplan(floorplan, document, graph, rules):
    """Pure objective function on validated canonical inputs; lower cost is better."""
    instances = {i["instance"]: i for i in floorplan["instances"]}
    def center(box):
        return tuple(Fraction(box[0][a] + box[1][a], 2) for a in (0, 1))

    boxes = [i["bbox"] for i in floorplan["instances"] + floorplan["pins"]]
    points = [p for b in boxes for p in b] + floorplan["pr_boundary"]
    bbox = [[min(p[a] for p in points) for a in (0, 1)], [max(p[a] for p in points) for a in (0, 1)]] if points else None
    width, height = (bbox[1][a] - bbox[0][a] for a in (0, 1)) if bbox else (0, 0)
    area = width * height if width > 0 and height > 0 else None
    ratio = width / height if area else None
    nets, mapped, missing = {}, set(), set()
    for device in graph["devices"]:
        name = instance_identifier(device["name"], allow_leading_digit=True)
        mapped.add(name)
        if name not in instances:
            missing.add(name)
        for net in set(device["terms"].values()):
            nets.setdefault(net, set()).add(name)
    pins = {}
    for pin in floorplan["pins"]:
        pins.setdefault(pin["name"], []).append(center(pin["bbox"]))
    per_net, skipped = {}, []
    for net in sorted(nets):
        names = nets[net]
        if names & missing:
            skipped.append(net)
            continue
        endpoints = [center(instances[n]["bbox"]) for n in sorted(names)] + pins.get(net, [])
        per_net[net] = float(sum(max(p[a] for p in endpoints) - min(p[a] for p in endpoints) for a in (0, 1)))
    unknown_pins = sorted(set(pins) - set(graph["nets"]))
    complete = not graph["warnings"] and not missing and not (set(instances) - mapped) and not unknown_pins
    hpwl = math.fsum(per_net.values())
    compliance = {}
    penalties = {}
    for kind in ("symmetry", "matching"):
        items = [c for c in document["constraints"] if c["kind"] == kind and (kind != "matching" or c["style"] != "none")]
        details = [{"id": c["id"], "warnings": check_placement(floorplan, {"constraints": [c]})} for c in items]
        passed = sum(not d["warnings"] for d in details)
        compliance[kind] = {"checked": len(items), "passed": passed, "fraction": passed / len(items) if items else None, "details": details}
        penalties[kind] = 1 - passed / len(items) if items else 0.0
    targets = rules.get("targets", {})
    area_scale = number_value(str(targets["area_nm2"]["max"])) if "area_nm2" in targets else 1
    aspect_cost = max(ratio, 1 / ratio) - 1 if ratio else None
    if ratio and "aspect_ratio" in targets:
        lo, hi = (number_value(str(targets["aspect_ratio"][k])) for k in ("min", "max"))
        aspect_cost = max(lo / ratio - 1, ratio / hi - 1, 0)
    target_violations = []
    for key, value in (("area_nm2", area), ("aspect_ratio", ratio)):
        if key in targets:
            lo, hi = (number_value(str(targets[key][k])) for k in ("min", "max"))
            if value is None or not lo <= value <= hi:
                target_violations.append(key)
    components = {"area": area / area_scale if area else None, "aspect_ratio": aspect_cost,
                  "wirelength": hpwl if complete else None, **penalties, "power": None}
    weights = rules.get("objective_weights", {})
    unavailable = sorted(k for k, w in weights.items() if number_value(str(w)) > 0 and components[k] is None)
    weighted = None if unavailable or not weights else math.fsum(number_value(str(w)) * components[k] for k, w in sorted(weights.items()) if number_value(str(w)) > 0)
    if weighted is not None and not math.isfinite(weighted):
        raise ValueError("Floorplan weighted cost is not finite")
    return {"schema_version": 1, "method": "bbox-center-hpwl-v1", "placement_sha256": placement_sha256(floorplan),
            "bbox_nm": bbox, "area_nm2": area, "aspect_ratio": ratio,
            "hpwl": {"estimate_nm": hpwl, "by_net_nm": per_net, "complete": complete,
                     "skipped_nets": skipped, "missing_placement": sorted(missing),
                     "missing_connectivity": sorted(set(instances) - mapped), "topology_warnings": graph["warnings"],
                     "unmapped_pins": unknown_pins,
                     "coverage": graph["coverage"], "endpoint_model": "instance/pin bbox centers, not device terminals"},
            "compliance": compliance, "components": components, "weights": weights, "weighted_cost": weighted,
            "unavailable_objectives": unavailable, "target_violations": target_violations,
            "targets_met": not target_violations if targets else None,
            "not_checked": ["real routed wirelength", "terminal positions", "DRC", "LVS", "electrical power", "noise", "distortion"]}


def floorplan_score(run: Path, *, source="candidate"):
    if source not in {"candidate", "live"}:
        raise ValueError("Score source must be candidate or live")
    run = layout_path(run)
    with layout_lock(run) as session:
        if read_json(local_path(run, "task.json"))["status"] not in EXPORT_STATES:
            raise ValueError("Cannot score a floorplan in the current state")
        try:
            cfg, _ = layout_context(run)
            constraints = frozen_constraints(run, session=session)
            floorplan, reference = reviewed_candidate(run, cfg) if source == "candidate" else (_capture(run, cfg), None)
            inputs_path = local_path(run, "layout_inputs.json")
            inputs = read_json(inputs_path)
            schematic = local_path(run.parent.parent, f"runs/{inputs['schematic']['run_id']}")
            net_path = local_path(schematic, read_json(local_path(schematic, "netlist_result.json"))["netlist"])
            raw = net_path.read_bytes()
            import hashlib
            if hashlib.sha256(raw).hexdigest() != inputs["schematic"]["netlist_sha256"]:
                raise LayoutEvidenceError("Scoring netlist no longer matches the linked simulation")
            graph = topology(raw.decode("utf-8"), cfg.get("detection", {}))
            rules_path = local_path(run.parent.parent, RULES_PATH)
            rules = validate_rules(read_json(rules_path), leading=cfg.get("allow_leading_digit", False)) if rules_path.exists() else {"targets": {}, "objective_weights": {}}
            result = score_floorplan(floorplan, constraints["document"], graph, rules)
            layout_context(run)
            if source == "candidate" and reviewed_candidate(run, cfg) != (floorplan, reference):
                raise LayoutEvidenceError("Scored candidate changed during evaluation")
            return {**result, "source": source, "candidate": reference,
                    "inputs_sha256": sha256(inputs_path), "netlist_sha256": inputs["schematic"]["netlist_sha256"],
                    "constraints_freeze": constraints["reference"], "rules_sha256": inputs["private_sha256"].get(RULES_PATH),
                    "status": read_json(local_path(run, "task.json"))["status"]}
        except Exception as exc:
            _attention(run, exc)
            raise
