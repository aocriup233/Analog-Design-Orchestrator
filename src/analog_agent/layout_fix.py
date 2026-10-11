"""Bounded explicit fix loop. A proposal never routes or verifies by itself."""

from __future__ import annotations

from .core import local_path, read_json
from .layout_constraints import _pinned
from .layout_floorplan import require_unchanged_floorplan
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .layout_routing import routing_context, validate_plan
from .layout_worker import attention, publish, route, snapshot
from .state import status, transition


def _failure(run):
    state = status(run)
    if state not in {"DRC_DONE", "LVS_DONE"}:
        raise ValueError("Fix requires a completed failing DRC/LVS handoff")
    task = read_json(local_path(run, "task.json"))
    stage = "drc" if state == "DRC_DONE" else "lvs"
    ref = task[f"active_{stage}_report"]
    report = _pinned(run, ref)
    from .layout_verification import repair_action
    if repair_action(report) != "routing":
        raise ValueError("Only explicitly classified routing violations may enter the fix loop")
    if report["clean"] or not report["violations"] or report["routing_result"] != task["active_routing_result"]:
        raise ValueError("No listed routing violations for the current result; manual attention required")
    for relative, expected in report["raw_reports"].items():
        from .core import sha256
        if sha256(local_path(run, relative)) != expected:
            raise LayoutEvidenceError("Fix violation report changed")
    return task, ref, report


def prepare_fix(run, session):
    session.require(run)
    cfg, _ = layout_context(run)
    task, ref, report = _failure(run)
    if task.get("fix_iterations", 0) >= cfg.get("max_fix_iterations", 3):
        transition(run, "NEEDS_ATTENTION", error={"kind": "FixLimit", "message": "Maximum layout fix iterations reached",
                   "last_report": ref, "remaining_violations": len(report["violations"])})
        raise ValueError("Maximum layout fix iterations reached; NEEDS_ATTENTION")
    if any(not v["nets"] for v in report["violations"]):
        raise ValueError("Unmapped violation cannot be auto-fixed; manual attention required")
    return publish(run, "fix_requests", {"report": ref, "routing_result": task["active_routing_result"],
        "cellview_sha256": report["cellview_sha256"], "violations": report["violations"],
        "affected_nets": sorted({net for v in report["violations"] for net in v["nets"]})})


def apply_fix(run):
    """Explicit operator action applies the reviewed proposal and re-verifies."""
    from .layout_verification import verify
    run = layout_path(run)
    with layout_lock(run) as session:
        cfg, _ = layout_context(run)
        task, ref, report = _failure(run)
        if task.get("fix_iterations", 0) >= cfg.get("max_fix_iterations", 3):
            transition(run, "NEEDS_ATTENTION", error={"kind": "FixLimit", "message": "Maximum layout fix iterations reached",
                       "last_report": ref, "remaining_violations": len(report["violations"])})
            raise ValueError("Maximum layout fix iterations reached; NEEDS_ATTENTION")
        fix = task.get("active_fix")
        if not fix:
            raise ValueError("Review a fix-propose JSON artifact before explicitly applying a fix")
        failure = _pinned(run, fix)
        if failure["report"] != ref or failure["routing_result"] != task["active_routing_result"] or failure["violations"] != report["violations"]:
            raise LayoutEvidenceError("Fix proposal is not bound to the current listed violations")
        _, _, context = routing_context(run, session, fix=fix)
        validate_plan(_pinned(run, task["active_routing_plan"]), context, cfg)
        require_unchanged_floorplan(run, "verification", session=session)
        try:
            if snapshot(run, cfg)["sha256"] != failure["cellview_sha256"]:
                raise LayoutEvidenceError("Cellview changed since the failing report")
            # This backedge is authorized only by a validated, explicitly applied
            # fix. Preserve all old receipts and increment the bounded counter.
            transition(run, "FLOORPLAN_FROZEN", fix_iterations=task.get("fix_iterations", 0) + 1,
                       applied_fix=fix)
            route(run, session=session, fix=fix)
            result = verify(run, session=session)
            if not result["clean"] and status(run) in {"DRC_DONE", "LVS_DONE"} and read_json(local_path(run, "task.json"))["fix_iterations"] >= cfg.get("max_fix_iterations", 3):
                transition(run, "NEEDS_ATTENTION", error={"kind": "FixLimit", "message": "Fix limit exhausted with verification violations",
                           "last_report": result["report"]})
                return {**result, "status": "NEEDS_ATTENTION"}
            return result
        except Exception as exc:
            attention(run, type(exc).__name__)
            raise
