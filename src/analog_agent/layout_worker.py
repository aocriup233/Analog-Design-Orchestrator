"""Deterministic owned-route execution, with one lock across all postconditions."""

from __future__ import annotations

import uuid
from pathlib import Path

from .core import local_path, read_json, sha256, update_status, write_json
from .layout_constraints import _pinned, digest
from .layout_executor import execute, configured
from .layout_floorplan import _frozen, require_unchanged_floorplan
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .layout_routing import fields, routing_context, validate_plan
from .state import status, transition


def attention(run, kind="ExecutionUncertain"):
    if status(run) not in {"NEEDS_ATTENTION", "FLOORPLAN_PENDING"}:
        transition(run, "NEEDS_ATTENTION", error={"kind": kind,
                   "message": "Layout execution/evidence is uncertain; reconcile before any retry"})


def snapshot(run, cfg):
    value = execute(run, "read_layout_snapshot", {})
    fields(value, {"schema_version", "target", "saved", "placement_sha256", "geometry", "vias", "nets", "pins",
                   "hierarchy", "params", "technology_sha256", "declared_inputs_sha256", "protected_sha256", "sha256"})
    if value["schema_version"] != 1 or value["saved"] is not True or value["target"] != {
            "library": cfg["library"], "cell": cfg["cell"], "view": "layout"}:
        raise LayoutEvidenceError("Complete saved snapshot has wrong target/status")
    if any(not isinstance(value[k], (dict, list)) for k in ("geometry", "vias", "nets", "pins", "hierarchy", "params")):
        raise LayoutEvidenceError("Full snapshot must cover geometry, vias, nets, pins, hierarchy and PCell parameters")
    for key in ("placement_sha256", "technology_sha256", "declared_inputs_sha256", "protected_sha256", "sha256"):
        import re
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise LayoutEvidenceError("Invalid complete snapshot hash")
    if value["sha256"] != digest({k: v for k, v in value.items() if k != "sha256"}):
        raise LayoutEvidenceError("Complete snapshot hash mismatch")
    frozen = _frozen(run, cfg)
    if value["placement_sha256"] != frozen["placement_sha256"]:
        raise LayoutEvidenceError("Full snapshot placement differs from the reviewed manual floorplan")
    return value


def publish(run, category, value):
    path = local_path(run, f"{category}/{uuid.uuid4().hex}.json")
    write_json(path, value)
    return {"path": path.relative_to(run).as_posix(), "sha256": sha256(path)}


def route(run: Path, *, session=None, fix=None):
    run = layout_path(run)
    if session is None:
        with layout_lock(run) as held:
            return route(run, session=held, fix=fix)
    session.require(run)
    if status(run) != "FLOORPLAN_FROZEN":
        raise ValueError("Routing requires FLOORPLAN_FROZEN and a reviewed JSON plan")
    try:
        cfg, _, context = routing_context(run, session, fix=fix)
        task = read_json(local_path(run, "task.json"))
        reference = task.get("active_routing_plan")
        if reference:
            proposal = _pinned(run, reference)
    except LayoutEvidenceError:
        attention(run, "LayoutEvidenceError")
        raise
    if not reference:
        raise ValueError("Validated routing proposal required; proposals are never auto-executed")
    plan = validate_plan(proposal, context, cfg)
    configured(run, "read_layout_snapshot")
    require_unchanged_floorplan(run, "routing", session=session)
    mutated = False
    try:
        before = snapshot(run, cfg)
        previous = _pinned(run, task["active_routing_result"]) if fix else None
        if previous and previous["cellview_sha256"] != before["sha256"]:
            raise LayoutEvidenceError("Cellview changed since verification; cannot reroute")
        if previous:
            owned = [r["id"] for r in previous["owned_routes"] if r["net"] in context["allowed_nets"]]
            mutated = True
            receipt = execute(run, "remove_owned_routes", {"previous_result": task["active_routing_result"],
                              "route_ids": owned, "expected_cellview_sha256": before["sha256"]})
            if receipt != {"removed_route_ids": owned}:
                raise LayoutEvidenceError("Owned-route deletion receipt mismatch")
        ownership = [] if not previous else [r for r in previous["owned_routes"] if r["net"] not in context["allowed_nets"]]
        for net in plan["net_order"]:
            for item in (r for r in plan["routes"] if r["net"] == net):
                owned_id = "r_" + digest({"plan": reference["sha256"], "id": item["id"]})[:24]
                mutated = True
                receipt = execute(run, "append_path", {"route_id": owned_id, "net": net, "layer": item["layer"],
                                  "width_nm": item["width_nm"], "spacing_nm": item["spacing_nm"],
                                  "shielding": item["shielding"], "points_nm": item["guide_points"]})
                if receipt != {"route_id": owned_id, "net": net, "created": True}:
                    raise LayoutEvidenceError("Typed path/net association receipt mismatch")
                for index, via in enumerate(item["vias"]):
                    via_id = f"{owned_id}_v{index}"
                    receipt = execute(run, "append_via", {"route_id": via_id, "net": net, **via})
                    if receipt != {"route_id": via_id, "net": net, "created": True}:
                        raise LayoutEvidenceError("Typed via association receipt mismatch")
                    ownership.append({"id": via_id, "net": net})
                ownership.append({"id": owned_id, "net": net})
        saved = execute(run, "save_target", {})
        # Pre/post placement checks are under the same lock. ADO never moves instances.
        require_unchanged_floorplan(run, "routing", session=session)
        after = snapshot(run, cfg)
        if saved != {"saved": True, "cellview_sha256": after["sha256"]}:
            raise LayoutEvidenceError("Save receipt does not bind the saved full snapshot")
        if any(before[k] != after[k] for k in ("protected_sha256", "technology_sha256", "declared_inputs_sha256")):
            raise LayoutEvidenceError("Routing changed protected non-owned geometry or declared inputs")
        terminals = execute(run, "read_terminals", {"expected_cellview_sha256": after["sha256"]})
        fields(terminals, {"cellview_sha256", "routed_nets", "unrouted_nets", "warnings"})
        routed, unrouted = terminals["routed_nets"], terminals["unrouted_nets"]
        if (terminals["cellview_sha256"] != after["sha256"] or not isinstance(routed, list) or not isinstance(unrouted, list)
                or not all(isinstance(n, str) for n in routed + unrouted)
                or set(routed) & set(unrouted) or len(set(routed + unrouted)) != len(routed + unrouted)
                or set(routed + unrouted) != set(context["nets"]) or not isinstance(terminals["warnings"], list)):
            raise LayoutEvidenceError("Terminal connectivity result does not cover the exact schematic net list")
        if snapshot(run, cfg)["sha256"] != after["sha256"]:
            raise LayoutEvidenceError("Cellview changed during connectivity readback")
        result = {"schema_version": 1, "plan": reference, "inputs": context["inputs"],
                  "cellview_sha256": after["sha256"], "snapshot": publish(run, "cellview_snapshots", after),
                  "placement_sha256": after["placement_sha256"], "owned_routes": ownership,
                  "routed_nets": routed, "unrouted_nets": unrouted, "warnings": terminals["warnings"],
                  "fix": fix}
        result_ref = publish(run, "routing_results", result)
        write_json(local_path(run, "routing_result.json"), result)
        transition(run, "ROUTED", active_routing_result=result_ref,
                   active_drc_report=None, active_lvs_report=None)
        return {**result_ref, "status": status(run), "unrouted_nets": unrouted}
    except Exception as exc:
        if mutated or isinstance(exc, (TimeoutError, LayoutEvidenceError, RuntimeError)):
            attention(run, type(exc).__name__)
        raise
