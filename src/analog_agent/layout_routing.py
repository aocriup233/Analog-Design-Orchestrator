"""JSON-only routing proposals. No Bridge client or foundry rules in contexts."""

from __future__ import annotations

import copy
import math
import uuid
from pathlib import Path

from .core import local_path, read_json, sha256, update_status, write_json
from .layout_config import identifier
from .layout_constraints import digest, frozen_constraints, topology, _pinned
from .layout_floorplan import _frozen, require_unchanged_floorplan
from .layout_runs import layout_context, layout_lock, layout_path
from .parameters import number_value, validate_value
from .state import status


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError(f"Expected JSON fields {sorted(required)}; unknown/missing fields rejected")


def numeric(value, label, *, positive=False):
    result = number_value(validate_value(label, value, {"type": "number"}))
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{label} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def validate_tech(tech):
    fields(tech, {"layers", "vias"})
    if not isinstance(tech["layers"], dict) or not tech["layers"] or not isinstance(tech["vias"], dict):
        raise ValueError("tech_summary requires layers and vias maps")
    for layer, rule in tech["layers"].items():
        identifier(layer, "layer")
        fields(rule, {"min_width_nm", "min_spacing_nm"})
        numeric(rule["min_width_nm"], "min_width_nm", positive=True)
        numeric(rule["min_spacing_nm"], "min_spacing_nm")
    for name, rule in tech["vias"].items():
        identifier(name, "via")
        fields(rule, {"layers", "min_enclosure_nm"})
        if (not isinstance(rule["layers"], list) or len(rule["layers"]) != 2
                or len(set(rule["layers"])) != 2 or not set(rule["layers"]) <= set(tech["layers"])):
            raise ValueError("Via requires two declared layers")
        numeric(rule["min_enclosure_nm"], "min_enclosure_nm")
    return copy.deepcopy(tech)


def _coordinate(value, grid):
    # Signed coordinates use the shared parameter validator, not a code blacklist.
    result = number_value(validate_value("coordinate_nm", value, {"type": "number"}))
    if abs(result - round(result)) > 1e-6 or abs(result / grid - round(result / grid)) * grid > 1e-6:
        raise ValueError("Routing coordinates must be integer nm on the manufacturing grid")
    return round(result)


def _point(value, grid):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Routing point must have two coordinates")
    return [_coordinate(v, grid) for v in value]


def validate_plan(plan, context, cfg):
    fields(plan, {"schema_version", "inputs", "net_order", "routes", "symmetric_pairs", "violation_ids"})
    if plan["schema_version"] != 1 or plan["inputs"] != context["inputs"]:
        raise ValueError("Routing plan must bind the exact reviewed context")
    tech = validate_tech(context["tech_summary"])
    grid = number_value(str(cfg["grid_nm"]))
    allowed = set(context["allowed_nets"])
    order = plan["net_order"]
    if (not isinstance(order, list) or not all(isinstance(n, str) for n in order)
            or len(order) != len(set(order)) or set(order) != allowed):
        raise ValueError("net_order must cover exactly allowed nets, without duplicates")
    if plan["violation_ids"] != context["violation_ids"]:
        raise ValueError("Fix proposals must address the pinned violation IDs only")
    if not isinstance(plan["routes"], list) or len(plan["routes"]) > 10000:
        raise ValueError("routes must be a bounded list")
    constraints = context["constraints"]["constraints"]
    expected_pairs = [pair for c in constraints if c["kind"] == "symmetric_routing" for pair in c["pairs"]
                      if set(pair) & allowed]
    if plan["symmetric_pairs"] != expected_pairs:
        raise ValueError("Symmetric routing pairs must match frozen constraints")
    seen = set()
    result = copy.deepcopy(plan)
    for route in result["routes"]:
        fields(route, {"id", "net", "layer", "width_nm", "spacing_nm", "shielding", "guide_points", "vias"})
        identifier(route["id"], "route id")
        if route["id"] in seen or route["net"] not in allowed or route["layer"] not in tech["layers"]:
            raise ValueError("Duplicate route ID, undeclared net or layer")
        seen.add(route["id"])
        rule = tech["layers"][route["layer"]]
        width = numeric(route["width_nm"], "width_nm", positive=True)
        spacing = numeric(route["spacing_nm"], "spacing_nm")
        _coordinate(width, grid)
        if width < numeric(rule["min_width_nm"], "min_width_nm") or spacing < numeric(rule["min_spacing_nm"], "min_spacing_nm"):
            raise ValueError("Route violates private numeric width/spacing limits")
        if route["shielding"] is not None and route["shielding"] not in context["nets"]:
            raise ValueError("Shielding must reference a declared net")
        for c in constraints:
            if c["kind"] == "net_class" and route["net"] in c["nets"]:
                if (width < c["width_nm"] or spacing < c["spacing_nm"] or
                        (c["layers"] and route["layer"] not in c["layers"]) or route["shielding"] != c["shielding"]):
                    raise ValueError("Route violates frozen net-class constraints")
        points = route["guide_points"]
        if not isinstance(points, list) or not 2 <= len(points) <= 10000:
            raise ValueError("Path requires a bounded list of at least two guide points")
        route["guide_points"] = [_point(p, grid) for p in points]
        for a, b in zip(route["guide_points"], route["guide_points"][1:]):
            if a == b or (a[0] != b[0] and a[1] != b[1]):
                raise ValueError("Only nonzero Manhattan path segments are supported")
            for c in constraints:
                if c["kind"] == "keep_out":
                    lo, hi = c["bbox_nm"]
                    if all(min(a[i], b[i]) - width / 2 < hi[i] and max(a[i], b[i]) + width / 2 > lo[i] for i in (0, 1)):
                        raise ValueError("Path width overlaps a frozen keep-out")
        if not isinstance(route["vias"], list) or len(route["vias"]) > 10000:
            raise ValueError("vias must be a bounded list")
        for via in route["vias"]:
            fields(via, {"via", "xy", "orientation"})
            if via["via"] not in tech["vias"] or via["orientation"] not in {"R0", "R90", "R180", "R270", "MX", "MY"}:
                raise ValueError("Unknown via or orientation")
            if route["layer"] not in tech["vias"][via["via"]]["layers"]:
                raise ValueError("Via does not land on the assigned layer")
            via["xy"] = _point(via["xy"], grid)
            if via["xy"] not in route["guide_points"]:
                raise ValueError("Via must be at an explicit guide point")
    for c in constraints:
        if c["kind"] == "max_vias":
            for net in c["nets"]:
                if sum(len(r["vias"]) for r in result["routes"] if r["net"] == net) > c["max_count"]:
                    raise ValueError("Route exceeds frozen maximum via count")
    return result


def routing_context(run, session, *, fix=None):
    session.require(run)
    cfg, project = layout_context(run)
    if "tech_summary" not in cfg:
        raise ValueError("Routing requires a private numeric tech_summary")
    constraints = frozen_constraints(run, session=session)
    floorplan = _frozen(run, cfg)
    task = read_json(local_path(run, "task.json"))
    inputs = read_json(local_path(run, "layout_inputs.json"))
    source = local_path(run.parent.parent, f"runs/{inputs['schematic']['run_id']}")
    net = read_json(local_path(source, "netlist_result.json"))
    text = local_path(source, net["netlist"]).read_text(encoding="utf-8")
    graph = topology(text, cfg.get("detection", {}))
    nets = sorted(graph["nets"])
    bindings = {"layout_inputs_sha256": sha256(local_path(run, "layout_inputs.json")),
                "floorplan_freeze": task["active_floorplan_freeze"],
                "constraints_freeze": constraints["reference"],
                "routing_review": task.get("active_routing_review"),
                "netlist_sha256": inputs["schematic"]["netlist_sha256"],
                "tech_sha256": digest(cfg["tech_summary"]), "fix": fix}
    allowed, violations = nets, []
    if fix is not None:
        failure = _pinned(run, fix)
        allowed = failure["affected_nets"]
        violations = [v["id"] for v in failure["violations"]]
        if not allowed or not set(allowed) <= set(nets):
            raise ValueError("Violation cannot be mapped to declared nets; manual attention required")
        for pair in [p for c in constraints["document"]["constraints"] if c["kind"] == "symmetric_routing" for p in c["pairs"]]:
            if set(pair) & set(allowed) and not set(pair) <= set(allowed):
                raise ValueError("Fix would change an unlisted symmetric partner; manual attention required")
    return cfg, project, {"schema_version": 1, "inputs": bindings,
        "floorplan": floorplan["floorplan"], "constraints": {k: constraints["document"][k] for k in ("schema_version", "constraints")},
        "netlist": text, "nets": nets, "allowed_nets": allowed, "tech_summary": copy.deepcopy(cfg["tech_summary"]),
        "violation_ids": violations, "violations": _pinned(run, fix)["violations"] if fix else []}


def propose_routing(run: Path):
    """Invoke only the trusted routing role; validate and pin JSON, never route."""
    from .workflow import invoke_json_role
    run = layout_path(run)
    with layout_lock(run) as session:
        if status(run) != "FLOORPLAN_FROZEN":
            raise ValueError("Routing proposal requires FLOORPLAN_FROZEN")
        require_unchanged_floorplan(run, "routing", session=session)
        cfg, project, context = routing_context(run, session)
        token = uuid.uuid4().hex
        directory = local_path(run, f"routing_proposals/{token}")
        directory.mkdir(parents=True, exist_ok=False)
        request, output = directory / "context.json", directory / "routing_plan.json"
        write_json(request, context)
        try:
            invoke_json_role("routing", project, request, output, directory)
        except TimeoutError:
            from .state import transition
            transition(run, "NEEDS_ATTENTION", error={"kind": "TimeoutError", "message": "Routing role timeout; reconcile before retry"})
            raise
        plan = validate_plan(read_json(local_path(run, str(output))), context, cfg)
        require_unchanged_floorplan(run, "routing", session=session)
        write_json(output, plan)
        reference = {"path": output.relative_to(run).as_posix(), "sha256": sha256(output)}
        update_status(run, status(run), active_routing_plan=reference)
        return {**reference, "status": status(run)}
