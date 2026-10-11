"""Manual placement snapshots and human freeze gate, without placement mutations."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from . import layout_bridge
from .core import local_path, read_json, sha256, update_status, utc_now, write_json
from .layout_config import identifier
from .layout_runs import layout_context, layout_lock, layout_path
from .parameters import number_value, validate_value
from .state import status, transition

ORIENTATIONS = {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}
UNITS = {"um", "nm", "m"}
EXPORT_STATES = {"LAYOUT_PREPARED", "CONSTRAINTS_DRAFTED", "CONSTRAINTS_FROZEN",
                 "FLOORPLAN_PENDING", "FLOORPLAN_FROZEN", "ROUTED", "DRC_DONE",
                 "LVS_DONE", "LAYOUT_VERIFIED"}


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Placement geometry must be finite JSON numbers in declared units")
    result = number_value(validate_value("geometry", value, {"type": "number"}))
    return 0.0 if result == 0 else result


def _object(value, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} must contain exactly {sorted(fields)}")
    return value


def validate_floorplan(document: dict, cfg: dict) -> dict:
    """Complete canonical schema; do not accept permissive/rounded Bridge dumps."""
    _object(document, {"schema_version", "source", "cellview", "units", "instances", "export_timestamp"}, "floorplan")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1 or document["source"] != "manual":
        raise ValueError("L1 requires schema_version=1 and source=manual")
    target = _object(document["cellview"], {"library", "cell", "view", "path"}, "cellview")
    expected = {"library": cfg["library"], "cell": cfg["cell"], "view": "layout",
                "path": f"{cfg['library']}/{cfg['cell']}/layout"}
    if target != expected:
        raise ValueError("Placement target does not match the configured cellview")
    if not isinstance(document["units"], str) or document["units"] not in UNITS:
        raise ValueError("Placement units must be um, nm or m, from authoritative cellview metadata")
    if not isinstance(document["export_timestamp"], str) or not document["export_timestamp"]:
        raise ValueError("Export timestamp is required")
    instances = document["instances"]
    if not isinstance(instances, list) or len(instances) > 100000:
        raise ValueError("Placement instances must be a bounded list")
    result, names = [], set()
    for item in instances:
        _object(item, {"instance", "master", "x", "y", "orientation", "bbox"}, "instance")
        name = identifier(item["instance"], "instance")
        if name in names:
            raise ValueError("Duplicate placement instance")
        names.add(name)
        master = _object(item["master"], {"library", "cell", "view"}, "master")
        for key, value in master.items():
            identifier(value, f"master.{key}")
        if not isinstance(item["orientation"], str) or item["orientation"] not in ORIENTATIONS:
            raise ValueError("Invalid placement orientation")
        bbox = item["bbox"]
        if (not isinstance(bbox, list) or len(bbox) != 2 or any(
                not isinstance(point, list) or len(point) != 2 for point in bbox)):
            raise ValueError("Placement bbox must be two xy corners")
        box = [[_number(value) for value in point] for point in bbox]
        if any(box[0][axis] > box[1][axis] for axis in (0, 1)):
            raise ValueError("Placement bbox corners are reversed")
        result.append({"instance": name, "master": dict(master), "x": _number(item["x"]),
                       "y": _number(item["y"]), "orientation": item["orientation"], "bbox": box})
    return {**document, "cellview": dict(target), "instances": sorted(result, key=lambda item: item["instance"])}


def placement_sha256(floorplan: dict) -> str:
    """Hash canonical placement content, not clock-dependent export metadata."""
    content = {key: value for key, value in floorplan.items() if key != "export_timestamp"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def _capture(run: Path, cfg: dict) -> dict:
    if not cfg.get("bridge", {}).get("reader"):
        raise ValueError("Complete typed placement reader is not configured; no raw SKILL fallback")
    raw = layout_bridge.read_placement(run, number_value(str(cfg.get("timeout_s", 300))))
    if not isinstance(raw, dict):
        raise ValueError("Placement reader must return a floorplan object")
    floorplan = validate_floorplan({**raw, "export_timestamp": utc_now()}, cfg)
    # Detect config/evidence changes during a slow Bridge read before publication.
    layout_context(run)
    return floorplan


def _frozen(run: Path, cfg: dict) -> dict:
    reference = read_json(local_path(run, "task.json")).get("active_floorplan_freeze")
    if not isinstance(reference, dict):
        raise ValueError("No active reviewed floorplan freeze")
    path = local_path(run, reference["path"])
    if sha256(path) != reference["sha256"]:
        raise ValueError("Frozen floorplan receipt changed")
    receipt = read_json(path)
    floorplan = validate_floorplan(receipt["floorplan"], cfg)
    if placement_sha256(floorplan) != receipt["placement_sha256"]:
        raise ValueError("Frozen placement hash mismatch")
    if receipt["inputs_sha256"] != sha256(local_path(run, "layout_inputs.json")):
        raise ValueError("Frozen placement input hash mismatch")
    return receipt


def _publish(run: Path, floorplan: dict) -> dict:
    path = local_path(run, "floorplan.json")
    write_json(path, floorplan)
    receipt = {"path": "floorplan.json", "sha256": sha256(path),
               "placement_sha256": placement_sha256(floorplan)}
    update_status(run, status(run), floorplan_export=receipt)
    return receipt


def _attention(run: Path, exc: Exception) -> None:
    if status(run) in EXPORT_STATES:
        # No raw plugin exception text or private content in the durable error.
        message = ("Placement reader timeout; reconcile the Bridge session before retry" if isinstance(exc, TimeoutError)
                   else "Layout evidence or placement reader failed; inspect inputs and reconcile the Bridge session before retry")
        transition(run, "NEEDS_ATTENTION", error={"kind": type(exc).__name__, "message": message})


def floorplan_export(run: Path) -> dict:
    run = layout_path(run)
    with layout_lock(run):
        current = status(run)
        if current not in EXPORT_STATES:
            raise ValueError(f"Cannot export placement from {current}")
        try:
            cfg, _ = layout_context(run)
            frozen = _frozen(run, cfg) if current in {"FLOORPLAN_FROZEN", "ROUTED", "DRC_DONE", "LVS_DONE", "LAYOUT_VERIFIED"} else None
            floorplan = _capture(run, cfg)
            receipt = _publish(run, floorplan)
            changed = frozen is not None and receipt["placement_sha256"] != frozen["placement_sha256"]
            if changed:
                transition(run, "FLOORPLAN_PENDING", active_floorplan_freeze=None,
                           message="Manual placement changed; routing/verification blocked until a new human freeze")
            return {**receipt, "status": status(run), "changed": changed}
        except Exception as exc:
            _attention(run, exc)
            raise


def floorplan_freeze(run: Path, reason: str) -> dict:
    run = layout_path(run)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Floorplan freeze requires a human review reason")
    with layout_lock(run):
        if status(run) != "FLOORPLAN_PENDING":
            raise ValueError("Freeze requires FLOORPLAN_PENDING, after the constraints human gate")
        try:
            cfg, _ = layout_context(run)
            export = read_json(local_path(run, "task.json")).get("floorplan_export", {})
            path = local_path(run, "floorplan.json")
            if export.get("path") != "floorplan.json" or sha256(path) != export.get("sha256"):
                raise ValueError("Review the current exported floorplan before freezing")
            reviewed = validate_floorplan(read_json(path), cfg)
            if placement_sha256(reviewed) != export.get("placement_sha256"):
                raise ValueError("Reviewed placement hash mismatch")
            live = _capture(run, cfg)
            if placement_sha256(live) != placement_sha256(reviewed):
                _publish(run, live)
                return {"status": "FLOORPLAN_PENDING", "changed": True,
                        "message": "Placement changed since export; review the new floorplan before freeze"}
            receipt = {"floorplan": reviewed, "floorplan_sha256": export["sha256"],
                       "placement_sha256": placement_sha256(reviewed),
                       "inputs_sha256": sha256(local_path(run, "layout_inputs.json")),
                       "reason": reason.strip(), "frozen_at": utc_now()}
            destination = local_path(run, f"floorplan_freezes/{uuid.uuid4().hex}.json")
            write_json(destination, receipt)
            reference = {"path": destination.relative_to(run).as_posix(), "sha256": sha256(destination)}
            transition(run, "FLOORPLAN_FROZEN", active_floorplan_freeze=reference)
            return {**reference, "status": "FLOORPLAN_FROZEN", "placement_sha256": receipt["placement_sha256"]}
        except Exception as exc:
            _attention(run, exc)
            raise


def require_unchanged_floorplan(run: Path, checkpoint: str) -> dict:
    """Mandatory future routing/verification preflight, not an execution command."""
    if checkpoint not in {"routing", "verification"}:
        raise ValueError("Floorplan checkpoint must be routing or verification")
    run = layout_path(run)
    allowed = {"FLOORPLAN_FROZEN"} if checkpoint == "routing" else {"ROUTED", "DRC_DONE", "LVS_DONE"}
    if status(run) not in allowed:
        raise ValueError(f"Invalid {checkpoint} floorplan checkpoint state")
    result = floorplan_export(run)
    if result["changed"]:
        raise ValueError("Manual placement changed: FLOORPLAN_PENDING; review and freeze again before proceeding")
    if result["status"] not in allowed:
        raise ValueError("Layout state changed during the checkpoint; inspect current evidence")
    return result
