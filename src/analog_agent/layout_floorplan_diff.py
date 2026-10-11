"""Read-only candidate/live comparison; never applies or invalidates placement."""
from pathlib import Path

from .core import local_path, read_json, sha256
from .layout_constraints import _pinned
from .layout_floorplan import _attention, _capture, placement_sha256, validate_floorplan, EXPORT_STATES
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path


def reviewed_candidate(run, cfg):
    task = read_json(local_path(run, "task.json"))
    generation = task.get("active_floorplan_generation")
    if generation:
        proof = _pinned(run, generation)
        candidate = validate_floorplan(proof["floorplan"], cfg)
        from .layout_floorplan_plugin import check_generation
        check_generation(run, candidate, generation)
        return candidate, generation
    reference = task.get("floorplan_export")
    if not reference:
        raise ValueError("Generate or export a candidate floorplan first")
    path = local_path(run, reference["path"])
    if sha256(path) != reference["sha256"]:
        raise LayoutEvidenceError("Reviewed candidate changed")
    candidate = validate_floorplan(read_json(path), cfg)
    if candidate["source"] == "plugin":
        from .layout_floorplan_plugin import check_generation
        check_generation(run, candidate, reference.get("generation"))
    return candidate, reference


def compare_floorplans(candidate, live):
    """Arguments are validated canonical integer-nm floorplans."""
    target = {i["instance"]: i for i in candidate["instances"]}
    actual = {i["instance"]: i for i in live["instances"]}
    differences = []
    for name in sorted(set(target) | set(actual)):
        wanted, current = target.get(name), actual.get(name)
        if wanted is None or current is None:
            differences.append({"kind": "instance", "instance": name,
                "action": "review_extra_instance" if wanted is None else "review_missing_instance",
                "live": current, "target": wanted})
            continue
        changed = sorted(k for k in wanted if wanted[k] != current[k])
        if changed:
            differences.append({"kind": "instance", "instance": name, "fields": changed,
                "action": "review_identity" if set(changed) & {"master", "params", "params_sha256"} else "move_rotate_or_review_bbox",
                "live": {k: current[k] for k in changed}, "target": {k: wanted[k] for k in changed},
                "target_origin_nm": [wanted["x"], wanted["y"]], "target_orientation": wanted["orientation"]})
    for field in ("cellview", "pins", "labels", "pr_boundary"):
        if candidate[field] != live[field]:
            differences.append({"kind": field, "action": "manual_review", "live": live[field], "target": candidate[field]})
    return {"schema_version": 1, "units": "nm", "empty": not differences,
            "candidate_placement_sha256": placement_sha256(candidate), "live_placement_sha256": placement_sha256(live),
            "differences": differences, "applied": False}


def floorplan_diff(run: Path):
    run = layout_path(run)
    with layout_lock(run):
        if read_json(local_path(run, "task.json"))["status"] not in EXPORT_STATES:
            raise ValueError("Cannot compare floorplans in the current state")
        try:
            cfg, _ = layout_context(run)
            candidate, reference = reviewed_candidate(run, cfg)
            live = _capture(run, cfg)
            # Recheck candidate evidence after the reader returns.
            current, current_ref = reviewed_candidate(run, cfg)
            if current != candidate or current_ref != reference:
                raise LayoutEvidenceError("Candidate changed during live comparison")
            return {**compare_floorplans(candidate, live), "candidate": reference,
                    "status": read_json(local_path(run, "task.json"))["status"]}
        except Exception as exc:
            _attention(run, exc)
            raise
