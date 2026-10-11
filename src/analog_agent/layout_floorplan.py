"""Manual placement snapshots and human freeze gate, without placement mutations."""

from __future__ import annotations

import hashlib
import json
import uuid
import re
from decimal import Decimal
from urllib.parse import quote, unquote
from pathlib import Path

from . import layout_bridge
from .core import local_path, read_json, sha256, update_status, utc_now, write_json
from .layout_config import identifier
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .parameters import number_value, validate_value
from .state import status, transition

ORIENTATIONS = {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}
UNITS = {"um": 1000, "nm": 1, "m": 1000000000}
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


def params_sha256(params: dict) -> str:
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        raise ValueError("Reported PCell parameters must be a JSON object")
    try:
        return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    except (ValueError, TypeError) as exc:
        raise ValueError("PCell parameters must be finite canonical JSON") from exc


def instance_identifier(value, *, allow_leading_digit=False) -> str:
    """ASCII hierarchy '/' and iteration '<i[:j]>' encoded as uppercase % escapes."""
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError(f"Invalid instance name: {value!r}")
    decoded = unquote(value)
    if "%" in value and quote(decoded, safe="_-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") != value:
        raise ValueError(f"Noncanonical escaped instance name: {value!r}")
    for segment in decoded.split("/"):
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:<(0|[1-9][0-9]*)(?::(0|[1-9][0-9]*))?>)?", segment)
        if not match:
            raise ValueError(f"Invalid instance name: {value!r}")
        identifier(match[1], "instance", allow_leading_digit=allow_leading_digit)
    return quote(decoded, safe="_-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def validate_floorplan(document: dict, cfg: dict) -> dict:
    """Schema 2: complete identity, integer nm, configured manufacturing grid."""
    _object(document, {"schema_version", "source", "cellview", "units", "instances", "export_timestamp",
                       "pins", "labels", "pr_boundary"}, "floorplan")
    if type(document["schema_version"]) is not int or document["schema_version"] != 2 or document["source"] != "manual":
        raise ValueError("L1 requires schema_version=2 and source=manual; legacy snapshots need a new run")
    target = _object(document["cellview"], {"library", "cell", "view", "path"}, "cellview")
    if not isinstance(document["units"], str) or document["units"] not in UNITS:
        raise ValueError("Placement units must be authoritative um, nm or m")
    if not isinstance(document["export_timestamp"], str) or not document["export_timestamp"]:
        raise ValueError("Export timestamp is required")
    instances = document["instances"]
    if not isinstance(instances, list) or len(instances) > 100000:
        raise ValueError("Placement instances must be a bounded list")
    leading = cfg.get("allow_leading_digit", False)
    offending = []
    def name(value, label, *, instance=False, view=False):
        try:
            return instance_identifier(value, allow_leading_digit=leading) if instance else identifier(
                value, label, allow_leading_digit=leading and not view)
        except ValueError:
            offending.append(f"{label}={value!r}")
            return value
    for key in ("library", "cell", "view"):
        name(target[key], f"cellview.{key}", view=key == "view")
    for item in instances:
        _object(item, {"instance", "master", "x", "y", "orientation", "bbox", "params", "params_sha256"}, "instance")
        name(item["instance"], "instance", instance=True)
        master = _object(item["master"], {"library", "cell", "view"}, "master")
        for key in master:
            name(master[key], f"master.{key}", view=key == "view")
    if offending:
        raise ValueError("Offending names: " + ", ".join(offending))
    expected = {"library": cfg["library"], "cell": cfg["cell"], "view": "layout",
                "path": f"{cfg['library']}/{cfg['cell']}/layout"}
    if target != expected:
        raise ValueError("Placement target does not match the configured cellview")
    grid = Decimal(str(number_value(validate_value("grid_nm", cfg.get("grid_nm"), {"type": "number"}))))
    tolerance = Decimal(str(number_value(validate_value("bbox_tolerance_nm", cfg.get("bbox_tolerance_nm", 0), {"type": "number"}))))
    if grid <= 0 or tolerance < 0:
        raise ValueError("Positive grid_nm and nonnegative bbox_tolerance_nm required")
    factor = UNITS[document["units"]]
    def coordinate(value):
        _number(value)
        nm = Decimal(str(value)) * factor
        integer = int(nm.to_integral_value())
        nearest_grid = (nm / grid).to_integral_value() * grid
        if abs(nm - nearest_grid) > Decimal("0.000001"):
            raise ValueError(f"Off-grid coordinate {value!r} in {document['units']}")
        if abs(nm - integer) > Decimal("0.000001"):
            raise ValueError("Coordinate is not representable as integer nanometres")
        return integer
    def point(value):
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("Expected an xy point")
        return [coordinate(v) for v in value]
    def box(value):
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("Expected two bbox corners")
        converted = [point(v) for v in value]
        if any(converted[0][axis] > converted[1][axis] for axis in (0, 1)):
            raise ValueError("Placement bbox corners are reversed")
        return converted
    result, names = [], set()
    for item in instances:
        label = instance_identifier(item["instance"], allow_leading_digit=leading)
        if label in names:
            raise ValueError(f"Duplicate placement instance: {label}")
        names.add(label)
        if not isinstance(item["orientation"], str) or item["orientation"] not in ORIENTATIONS:
            raise ValueError(f"Invalid placement orientation: {label}")
        bbox, x, y = box(item["bbox"]), coordinate(item["x"]), coordinate(item["y"])
        if any(not (bbox[0][axis] - tolerance <= origin <= bbox[1][axis] + tolerance)
               for axis, origin in enumerate((x, y))):
            raise ValueError(f"Placement origin outside bbox: {label}")
        digest = params_sha256(item["params"])
        if item["params_sha256"] != digest:
            raise ValueError(f"Reported PCell parameter hash mismatch: {label}")
        result.append({**item, "instance": label, "x": x, "y": y, "bbox": bbox})
    def shapes(values, kind):
        if not isinstance(values, list) or len(values) > 100000:
            raise ValueError(f"{kind} must be a bounded list")
        converted = []
        for item in values:
            field = "name" if kind == "pins" else "text"
            _object(item, {field, "layer", "bbox"}, kind)
            if not isinstance(item[field], str) or not item[field] or len(item[field]) > 4096:
                raise ValueError(f"Invalid {kind} text/name")
            identifier(item["layer"], f"{kind}.layer")
            converted.append({**item, "bbox": box(item["bbox"])})
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    boundary = document["pr_boundary"]
    if not isinstance(boundary, list) or (boundary and len(boundary) < 3) or len(boundary) > 100000:
        raise ValueError("pr_boundary must be an empty or at-least-three-point polygon")
    return {**document, "units": "nm", "instances": sorted(result, key=lambda item: item["instance"]),
            "pins": shapes(document["pins"], "pins"), "labels": shapes(document["labels"], "labels"),
            "pr_boundary": [point(v) for v in boundary]}


def placement_sha256(floorplan: dict) -> str:
    """Hash canonical placement content, not clock-dependent export metadata."""
    if floorplan.get("units") != "nm" or floorplan.get("schema_version") != 2:
        raise ValueError("placement_sha256 requires validate_floorplan's integer-nm canonical form")
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


def _frozen_unchecked(run: Path, cfg: dict) -> dict:
    reference = read_json(local_path(run, "task.json")).get("active_floorplan_freeze")
    if not isinstance(reference, dict):
        raise ValueError("No active reviewed floorplan freeze")
    path = local_path(run, reference["path"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("Frozen floorplan receipt changed")
    receipt = json.loads(raw)
    floorplan = validate_floorplan(receipt["floorplan"], cfg)
    if placement_sha256(floorplan) != receipt["placement_sha256"]:
        raise ValueError("Frozen placement hash mismatch")
    if receipt["inputs_sha256"] != sha256(local_path(run, "layout_inputs.json")):
        raise ValueError("Frozen placement input hash mismatch")
    constraint_ref = read_json(local_path(run, "task.json")).get("active_constraints_freeze")
    if (not isinstance(constraint_ref, dict) or receipt.get("constraints_freeze") != constraint_ref
            or sha256(local_path(run, constraint_ref["path"])) != constraint_ref["sha256"]):
        raise ValueError("Frozen placement constraint link is missing or changed")
    from .layout_constraints import _pinned
    check = _pinned(run, receipt.get("placement_check"))
    if (check.get("placement_sha256") != receipt["placement_sha256"] or
            check.get("constraints_freeze") != constraint_ref or check.get("inputs_sha256") != receipt["inputs_sha256"]):
        raise ValueError("Frozen placement check identity mismatch")
    return receipt


def _frozen(run: Path, cfg: dict) -> dict:
    try:
        return _frozen_unchecked(run, cfg)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise LayoutEvidenceError(str(exc)) from exc


def _publish(run: Path, floorplan: dict) -> dict:
    try:
        path = local_path(run, "floorplan.json")
    except ValueError as exc:
        raise LayoutEvidenceError("Export path was redirected outside the run") from exc
    write_json(path, floorplan)
    receipt = {"path": "floorplan.json", "sha256": sha256(path),
               "placement_sha256": placement_sha256(floorplan)}
    update_status(run, status(run), floorplan_export=receipt)
    return receipt


def _attention(run: Path, exc: Exception) -> None:
    if isinstance(exc, (TimeoutError, layout_bridge.ReaderStateError, LayoutEvidenceError)) and status(run) in EXPORT_STATES:
        # No raw plugin exception text or private content in the durable error.
        message = ("Placement reader timeout; reconcile the Bridge session before retry" if isinstance(exc, TimeoutError)
                   else "Layout evidence or placement reader failed; inspect inputs and reconcile the Bridge session before retry")
        error = {"kind": type(exc).__name__, "message": message}
        if getattr(exc, "diagnostic_log", None):
            relative = exc.diagnostic_log
            if local_path(run, relative).is_relative_to(local_path(run, "private_logs")):
                error["diagnostic_log"] = relative
        transition(run, "NEEDS_ATTENTION", error=error)


def floorplan_export(run: Path) -> dict:
    run = layout_path(run)
    with layout_lock(run) as session:
        return _floorplan_export_locked(run, session)


def _floorplan_export_locked(run: Path, session) -> dict:
    session.require(run)
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
                       active_routing_review=None,
                       message="Manual placement changed; routing/verification blocked until a new human freeze")
        return {**receipt, "status": status(run), "changed": changed}
    except Exception as exc:
        _attention(run, exc)
        raise


def floorplan_freeze(run: Path, reason: str) -> dict:
    run = layout_path(run)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Floorplan freeze requires a human review reason")
    with layout_lock(run) as session:
        if status(run) != "FLOORPLAN_PENDING":
            raise ValueError("Freeze requires FLOORPLAN_PENDING, after the constraints human gate")
        try:
            cfg, _ = layout_context(run)
            from .layout_constraints import frozen_constraints
            constraints = frozen_constraints(run, session=session)
            export = read_json(local_path(run, "task.json")).get("floorplan_export", {})
            if not export:
                raise ValueError("Export and review a floorplan before freezing")
            path = local_path(run, "floorplan.json")
            if export.get("path") != "floorplan.json" or not path.is_file() or sha256(path) != export.get("sha256"):
                raise LayoutEvidenceError("Review the current exported floorplan before freezing; evidence changed")
            try:
                reviewed = validate_floorplan(read_json(path), cfg)
            except (ValueError, OSError) as exc:
                raise LayoutEvidenceError("Reviewed placement evidence is invalid") from exc
            if placement_sha256(reviewed) != export.get("placement_sha256"):
                raise LayoutEvidenceError("Reviewed placement hash mismatch")
            from .layout_placement_check import build_check_report, publish_check_snapshot
            report = build_check_report(reviewed, constraints, sha256(local_path(run, "layout_inputs.json")))
            live = _capture(run, cfg)
            if placement_sha256(live) != placement_sha256(reviewed):
                _publish(run, live)
                return {"status": "FLOORPLAN_PENDING", "changed": True,
                        "message": "Placement changed since export; review the new floorplan before freeze"}
            check_reference = publish_check_snapshot(run, report)
            receipt = {"floorplan": reviewed, "floorplan_sha256": export["sha256"],
                       "placement_sha256": placement_sha256(reviewed),
                       "inputs_sha256": sha256(local_path(run, "layout_inputs.json")),
                       "constraints_freeze": constraints["reference"],
                       "placement_check": check_reference,
                       "reason": reason.strip(), "frozen_at": utc_now()}
            destination = local_path(run, f"floorplan_freezes/{uuid.uuid4().hex}.json")
            write_json(destination, receipt)
            reference = {"path": destination.relative_to(run).as_posix(), "sha256": sha256(destination)}
            transition(run, "FLOORPLAN_FROZEN", active_floorplan_freeze=reference, active_routing_review=None)
            from .layout_placement_check import floorplan_constraints_check
            checked = floorplan_constraints_check(run, session=session)
            return {**reference, "status": "FLOORPLAN_FROZEN", "placement_sha256": receipt["placement_sha256"],
                    "constraint_warnings": checked["warnings"]}
        except Exception as exc:
            _attention(run, exc)
            raise


def require_unchanged_floorplan(run: Path, checkpoint: str, *, session=None) -> dict:
    """Mandatory future routing/verification preflight, not an execution command."""
    if checkpoint not in {"routing", "verification"}:
        raise ValueError("Floorplan checkpoint must be routing or verification")
    run = layout_path(run)
    if session is None:
        with layout_lock(run) as held:
            return require_unchanged_floorplan(run, checkpoint, session=held)
    session.require(run)
    allowed = {"FLOORPLAN_FROZEN"} if checkpoint == "routing" else {"ROUTED", "DRC_DONE", "LVS_DONE"}
    if status(run) not in allowed:
        raise ValueError(f"Invalid {checkpoint} floorplan checkpoint state")
    from .layout_constraints import frozen_constraints
    try:
        frozen_constraints(run, session=session)
    except Exception as exc:
        _attention(run, exc)
        raise
    result = _floorplan_export_locked(run, session)
    if result["changed"]:
        raise ValueError("Manual placement changed: FLOORPLAN_PENDING; review and freeze again before proceeding")
    if result["status"] not in allowed:
        raise ValueError("Layout state changed during the checkpoint; inspect current evidence")
    if checkpoint == "routing":
        from .layout_placement_check import require_routing_review
        try:
            result["routing_review"] = require_routing_review(run, session=session)
        except Exception as exc:
            _attention(run, exc)
            raise
    return result
