"""Read-only geometric checks; warnings never authorize placement mutation."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import uuid

from .core import local_path, read_json, sha256, update_status, utc_now, write_json
from .layout_constraints import frozen_constraints, digest, _pinned
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .state import status
from .input_gate import checked_stage


def check_placement(floorplan, document):
    instances = {i["instance"]: i for i in floorplan["instances"]}
    warnings = []

    def warn(item, message, names):
        warnings.append({"constraint_id": item["id"], "source": item["source"],
                         "instances": sorted(names), "message": message})

    def center(instance):
        return tuple((Decimal(instance["bbox"][0][a]) + Decimal(instance["bbox"][1][a])) / 2 for a in (0, 1))

    def members(item, names):
        missing = [name for name in names if name not in instances]
        if missing:
            warn(item, "Constraint references instances absent from the floorplan", missing)
            return None
        return [instances[name] for name in names]

    for item in document["constraints"]:
        kind = item["kind"]
        if kind == "symmetry":
            axis = 0 if item["axis"]["direction"] == "x" else 1
            position = Decimal(str(item["axis"]["coordinate_nm"]))
            tolerance = Decimal(str(item["tolerance_nm"]))
            for names in item["pairs"]:
                pair = members(item, names)
                if pair is None:
                    continue
                a, b = map(center, pair)
                reflected = [a[0], a[1]]
                reflected[axis] = 2 * position - a[axis]
                sizes = [tuple(i["bbox"][1][j] - i["bbox"][0][j] for j in (0, 1)) for i in pair]
                if any(abs(reflected[j] - b[j]) > tolerance or abs(sizes[0][j] - sizes[1][j]) > tolerance for j in (0, 1)):
                    warn(item, "Bbox centers or extents violate the declared symmetry axis", names)
        elif kind == "matching":
            groups = [members(item, names) for names in item["groups"]]
            if any(group is None for group in groups):
                continue
            if item["style"] == "none":
                continue
            names = [name for group in item["groups"] for name in group]
            tolerance = Decimal(str(item["tolerance_nm"]))
            flat = [i for group in groups for i in group]
            identities = {(i["master"]["library"], i["master"]["cell"], i["params_sha256"],
                           tuple(i["bbox"][1][a] - i["bbox"][0][a] for a in (0, 1))) for i in flat}
            if len(identities) != 1:
                warn(item, "Matching units have different masters, parameters or bbox extents", names)
            if len(groups) < 2:
                warn(item, "Pattern checking requires at least two explicitly partitioned matching groups", names)
                continue
            if len({center(i) for i in flat}) != len(flat):
                warn(item, "Matching units have coincident bbox centers", names)
            if item["style"] == "common-centroid":
                centroids = [tuple(sum(center(i)[a] for i in group) / len(group) for a in (0, 1)) for group in groups]
                if any(abs(c[a] - centroids[0][a]) > tolerance for c in centroids[1:] for a in (0, 1)):
                    warn(item, "Matching groups do not share a common centroid", names)
                for group, centroid in zip(groups, centroids):
                    points = sorted(center(i) for i in group)
                    unused = list(points)
                    for point in points:
                        target = tuple(2 * centroid[a] - point[a] for a in (0, 1))
                        partner = next((p for p in unused if all(abs(p[a] - target[a]) <= tolerance for a in (0, 1))), None)
                        if partner is None:
                            warn(item, "Common-centroid group lacks a centrosymmetric unit pattern", [i["instance"] for i in group])
                            break
                        unused.remove(partner)
            else:
                ordered = sorted((center(i)[0], center(i)[1], n) for n, i in zip(names, flat))
                ys = [p[1] for p in ordered]
                labels = {name: index for index, group in enumerate(item["groups"]) for name in group}
                if max(ys) - min(ys) > tolerance or any(labels[a[2]] == labels[b[2]] for a, b in zip(ordered, ordered[1:])):
                    warn(item, "Interdigitated groups must alternate along one horizontal row", names)
        elif kind == "keep_out":
            box = item["bbox_nm"]
            for name, instance in sorted(instances.items()):
                b = instance["bbox"]
                if all(b[0][a] < box[1][a] and b[1][a] > box[0][a] for a in (0, 1)):
                    warn(item, "Instance bbox overlaps keep-out interior", [name])
    return warnings


def build_check_report(floorplan, constraints, inputs_sha256):
    from .layout_floorplan import placement_sha256
    names = {i["instance"] for i in floorplan["instances"]}
    for item in constraints["document"]["constraints"]:
        references = ([n for group in item["groups"] for n in group] if item["kind"] == "matching" else
                      [n for pair in item["pairs"] for n in pair] if item["kind"] == "symmetry" else [])
        missing = sorted(set(references) - names)
        if missing and item["source"] == "user":
            raise ValueError(f"Orphan floorplan references in {item['id']}: {', '.join(missing)}")
    warnings = check_placement(floorplan, constraints["document"])
    return {"schema_version": 2, "status": "WARNINGS" if warnings else "NO_WARNINGS",
            "warnings": warnings, "warnings_sha256": digest(warnings),
            "placement_sha256": placement_sha256(floorplan), "inputs_sha256": inputs_sha256,
            "constraints_freeze": constraints["reference"],
            "not_checked": ["routing", "foundry DRC", "LVS", "higher-order matching", "orientation", "device-internal geometry"]}


def publish_check_snapshot(run, report):
    path = local_path(run, f"placement_checks/{uuid.uuid4().hex}.json")
    write_json(path, report)
    return {"path": path.relative_to(run).as_posix(), "sha256": sha256(path)}


@checked_stage("layout_constraints_check")
def floorplan_constraints_check(run: Path, *, session=None):
    run = layout_path(run)
    if session is None:
        with layout_lock(run) as held:
            return floorplan_constraints_check(run, session=held)
    session.require(run)
    if status(run) != "FLOORPLAN_FROZEN":
        raise ValueError("Constraint placement check requires FLOORPLAN_FROZEN")
    try:
        from .layout_floorplan import _frozen
        cfg, _ = layout_context(run)
        constraints = frozen_constraints(run, session=session)
        floorplan = _frozen(run, cfg)
        if floorplan.get("constraints_freeze") != constraints["reference"]:
            raise LayoutEvidenceError("Floorplan was frozen against different constraints")
        report = build_check_report(floorplan["floorplan"], constraints, floorplan["inputs_sha256"])
        ref = floorplan.get("placement_check")
        if not isinstance(ref, dict) or _pinned(run, ref) != report:
            raise LayoutEvidenceError("Frozen placement check is missing or differs from recomputed evidence")
        path = local_path(run, "floorplan_check.json")
        write_json(path, report)
        update_status(run, status(run), floorplan_constraint_check=ref)
        return {**ref, "status": report["status"], "warnings": report["warnings"]}
    except LayoutEvidenceError as exc:
        from .layout_constraints import _constraint_attention
        _constraint_attention(run, exc)
        raise


def _routing_identity(run, checked):
    task = read_json(local_path(run, "task.json"))
    return {"floorplan_freeze": task["active_floorplan_freeze"],
            "constraints_freeze": task["active_constraints_freeze"],
            "placement_check": {k: checked[k] for k in ("path", "sha256")},
            "warnings_sha256": digest(checked["warnings"])}


@checked_stage("layout_routing_review")
def routing_review(run: Path, decision: str, reason: str, *, session=None):
    """Human decision only; records authorization, never starts L3."""
    run = layout_path(run)
    if decision not in {"proceed", "proceed-with-warnings"} or not isinstance(reason, str) or not reason.strip():
        raise ValueError("Routing review needs an explicit decision and reason")
    if session is None:
        with layout_lock(run) as held:
            return routing_review(run, decision, reason, session=held)
    session.require(run)
    if status(run) != "FLOORPLAN_FROZEN":
        raise ValueError("Routing review requires FLOORPLAN_FROZEN")
    floorplan_constraints_check(run, session=session)
    from .layout_floorplan import _floorplan_export_locked
    live = _floorplan_export_locked(run, session)
    if live["changed"]:
        raise ValueError("Placement changed: FLOORPLAN_PENDING; re-freeze before routing review")
    checked = floorplan_constraints_check(run, session=session)
    expected = "proceed-with-warnings" if checked["warnings"] else "proceed"
    if decision != expected:
        raise ValueError(f"Current placement check requires explicit decision {expected}")
    receipt = {**_routing_identity(run, checked), "decision": decision, "reason": reason.strip(),
               "reviewed_at": utc_now()}
    path = local_path(run, f"routing_reviews/{uuid.uuid4().hex}.json")
    write_json(path, receipt)
    ref = {"path": path.relative_to(run).as_posix(), "sha256": sha256(path)}
    update_status(run, status(run), active_routing_review=ref)
    return {**ref, "status": status(run), "decision": decision}


def require_routing_review(run, *, session):
    session.require(run)
    checked = floorplan_constraints_check(run, session=session)
    reference = read_json(local_path(run, "task.json")).get("active_routing_review")
    if not isinstance(reference, dict):
        raise ValueError("Routing blocked: explicit layout routing-review decision required")
    receipt = _pinned(run, reference)
    expected = "proceed-with-warnings" if checked["warnings"] else "proceed"
    if (any(receipt.get(k) != v for k, v in _routing_identity(run, checked).items()) or
            receipt.get("decision") != expected or not isinstance(receipt.get("reason"), str) or not receipt["reason"].strip()):
        raise LayoutEvidenceError("Routing review does not match the current frozen check and warning set")
    return reference
