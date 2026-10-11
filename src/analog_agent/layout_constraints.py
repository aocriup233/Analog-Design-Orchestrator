"""Deterministic, private layout constraints; no Bridge or executable proposals."""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import uuid
from pathlib import Path

from . import memory
from .core import local_path, read_json, sha256, update_status, utc_now, write_json
from .layout_config import identifier
from .layout_floorplan import instance_identifier
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .parameters import number_value, validate_value
from .state import status, transition

USER_PATH = "private/layout/constraints.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def numeric(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Constraint dimensions must be finite JSON numbers in nm")
    result = number_value(validate_value("constraint dimension", value, {"type": "number"}))
    if result < 0 or (positive and result == 0):
        raise ValueError("Constraint dimension outside bounds")
    return result


def validate_constraints(document, *, leading=False):
    if not isinstance(document, dict) or set(document) != {"schema_version", "constraints"} or document["schema_version"] != 1:
        raise ValueError("Constraints require schema_version 1 and constraints array")
    if not isinstance(document["constraints"], list) or len(document["constraints"]) > 10000:
        raise ValueError("Invalid constraints array")
    result, ids = [], set()
    fields = {
        "symmetry": {"pairs", "axis", "tolerance_nm"},
        "matching": {"groups", "style", "topology", "tolerance_nm"},
        "net_class": {"nets", "class", "width_nm", "spacing_nm", "shielding", "layers"},
        "keep_out": {"bbox_nm"},
        "symmetric_routing": {"pairs"},
        "max_vias": {"nets", "max_count"},
    }
    for original in document["constraints"]:
        if not isinstance(original, dict) or original.get("kind") not in fields:
            raise ValueError("Unknown constraint kind")
        item = json.loads(json.dumps(original, allow_nan=False))
        kind = item["kind"]
        if set(item) != {"id", "kind", "source", "rationale"} | fields[kind]:
            raise ValueError(f"Invalid fields for constraint {item.get('id')}")
        identifier(item["id"], "constraint id")
        if item["id"] in ids:
            raise ValueError("Duplicate constraint id")
        ids.add(item["id"])
        source = item["source"]
        if not isinstance(source, str) or not (source in {"rule", "user"} or re.fullmatch(r"memory:[A-Za-z][A-Za-z0-9_-]{0,63}", source)):
            raise ValueError("Constraint source must be rule, user or memory:ID")
        if not isinstance(item["rationale"], str) or not 1 <= len(item["rationale"].strip()) <= 1000:
            raise ValueError("Constraint rationale required")

        def names(values, instances=False, minimum=1):
            if not isinstance(values, list) or len(values) < minimum or not all(isinstance(v, str) for v in values):
                raise ValueError("Invalid constraint names")
            canonical = [(instance_identifier(v, allow_leading_digit=leading) if instances
                          else instance_identifier(v, allow_leading_digit=True)) for v in values]
            if len(set(canonical)) != len(canonical):
                raise ValueError("Duplicate constraint names")
            return canonical

        if kind in {"symmetry", "symmetric_routing"}:
            if not isinstance(item["pairs"], list) or not item["pairs"]:
                raise ValueError("Pairs required")
            for pair in item["pairs"]:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError("Pair must have two names")
            item["pairs"] = [names(p, kind == "symmetry", 2) for p in item["pairs"]]
        if kind == "symmetry":
            axis = item["axis"]
            if not isinstance(axis, dict) or set(axis) != {"direction", "coordinate_nm"} or axis["direction"] not in {"x", "y"}:
                raise ValueError("Symmetry requires an explicit x/y axis")
            # Axes may lie at negative coordinates.
            number_value(validate_value("axis", axis["coordinate_nm"], {"type": "number"}))
            if isinstance(axis["coordinate_nm"], bool) or not isinstance(axis["coordinate_nm"], (int, float)):
                raise ValueError("Axis coordinate must be a JSON number")
        if kind == "matching":
            if item["style"] not in {"common-centroid", "interdigitated", "none"}:
                raise ValueError("Unknown matching style")
            if item["topology"] not in {"differential_pair", "current_mirror", "cascode", "resistor", "capacitor", "user"}:
                raise ValueError("Unknown matching topology")
            groups = item["groups"]
            if not isinstance(groups, list) or not groups:
                raise ValueError("Matching groups required")
            item["groups"] = [names(g, True, 1) for g in groups]
            flat = list(itertools.chain.from_iterable(item["groups"]))
            if len(flat) != len(set(flat)) or len(flat) < 2:
                raise ValueError("Matching groups must contain distinct instances")
        if kind in {"symmetry", "matching"}:
            numeric(item["tolerance_nm"])
        if kind in {"net_class", "max_vias"}:
            item["nets"] = names(item["nets"])
        if kind == "net_class":
            if item["class"] not in {"critical", "sensitive", "power", "ground"}:
                raise ValueError("Unknown net class")
            numeric(item["width_nm"], positive=True)
            numeric(item["spacing_nm"])
            if item["shielding"] is not None:
                names([item["shielding"]])
            if not isinstance(item["layers"], list) or not item["layers"]:
                raise ValueError("Layer preferences required")
            for layer in item["layers"]:
                identifier(layer, "layer")
        if kind == "max_vias" and (type(item["max_count"]) is not int or item["max_count"] < 0):
            raise ValueError("max_count must be a nonnegative integer")
        if kind == "keep_out":
            box = item["bbox_nm"]
            if not isinstance(box, list) or len(box) != 2 or any(not isinstance(p, list) or len(p) != 2 for p in box):
                raise ValueError("Invalid keep-out bbox")
            for point in box:
                for value in point:
                    if isinstance(value, bool) or not isinstance(value, (float, int)):
                        raise ValueError("Keep-out coordinates must be JSON numbers")
                    number_value(validate_value("bbox", value, {"type": "number"}))
            if any(box[0][i] > box[1][i] for i in (0, 1)):
                raise ValueError("Reversed keep-out bbox")
        result.append(item)
    return {"schema_version": 1, "constraints": sorted(result, key=lambda c: c["id"])}


def topology(text, detection=None):
    """Conservative parenthesized Spectre subset; never follows include files."""
    detection = detection or {}
    models = {"resistor": {"kind": "resistor", "terminals": ["p", "n"]},
              "capacitor": {"kind": "capacitor", "terminals": ["p", "n"]}}
    models.update(detection.get("models", {}))
    blocks, current, warnings = {"": {"ports": [], "devices": []}}, "", []
    logical = text.replace("\\\n", " ")
    for lineno, line in enumerate(logical.splitlines(), 1):
        line = line.split("//", 1)[0].strip()
        if not line or line.startswith("*"):
            continue
        header = re.fullmatch(r"subckt\s+(\w+)\s*\(([^)]*)\)", line)
        if header:
            if current or header[1] in blocks:
                raise ValueError("Nested or duplicate subckt unsupported")
            current = header[1]
            blocks[current] = {"ports": header[2].split(), "devices": []}
            continue
        if line.startswith("ends"):
            if not current:
                raise ValueError("Unbalanced ends")
            current = ""
            continue
        match = re.fullmatch(r"([^\s()]+)\s*\(([^)]*)\)\s+(\w+)\s*(.*)", line)
        if match:
            params = match[4].split()
            if any(not re.fullmatch(r"\w+=[^\s]+", p) for p in params):
                warnings.append(f"Unsupported parameter syntax at line {lineno}; instance not inferred")
                continue
            blocks[current]["devices"].append({"name": match[1], "nodes": match[2].split(),
                "model": match[3], "params": dict(p.split("=", 1) for p in params)})
        elif not line.startswith(("simulator ", "parameters ", "global ")):
            warnings.append(f"Unsupported statement at line {lineno}; not inferred")
    if current:
        raise ValueError("Unterminated subckt")
    leaves = []

    def expand(block, prefix, mapping, stack):
        if block in stack or len(stack) >= 32:
            raise ValueError("Recursive or excessive netlist hierarchy")
        seen = set()
        for device in blocks[block]["devices"]:
            name = prefix + device["name"]
            instance_identifier(name, allow_leading_digit=True)
            if name in seen:
                raise ValueError("Duplicate netlist instance")
            seen.add(name)
            nodes = [mapping.get(n, prefix + n) for n in device["nodes"]]
            if device["model"] in blocks:
                sub = blocks[device["model"]]
                if device["params"] or len(nodes) != len(sub["ports"]):
                    warnings.append(f"Parameterized or mismatched subckt {name}; not inferred")
                    continue
                expand(device["model"], name + "/", dict(zip(sub["ports"], nodes)), stack + [block])
            else:
                definition = models.get(device["model"])
                if definition is None:
                    warnings.append(f"Unmapped device {name}; not inferred")
                    continue
                if len(nodes) != len(definition["terminals"]):
                    raise ValueError(f"Terminal count mismatch: {name}")
                leaves.append({**device, "name": name, "kind": definition["kind"],
                               "terms": dict(zip(definition["terminals"], nodes))})
    top = detection.get("top", "")
    if top not in blocks:
        raise ValueError("Configured detection top not found")
    expand(top, "", {n: n for n in blocks[top]["ports"]}, [])
    return {"devices": sorted(leaves, key=lambda d: d["name"]), "warnings": sorted(set(warnings))}


def detect_constraints(graph):
    detected = []
    for a, b in itertools.combinations(graph["devices"], 2):
        if a["kind"] != b["kind"] or a["model"] != b["model"] or a["params"] != b["params"]:
            continue
        kinds = []
        if a["kind"] in {"resistor", "capacitor"}:
            kinds.append(a["kind"])
        elif a["kind"] in {"mos", "bjt"}:
            control, output, common = ("g", "d", "s") if a["kind"] == "mos" else ("b", "c", "e")
            at, bt = a["terms"], b["terms"]
            if at[common] == bt[common] and at[control] != bt[control] and at[output] != bt[output]:
                kinds.append("differential_pair")
            if at[common] == bt[common] and at[control] == bt[control] and (at[control] == at[output] or bt[control] == bt[output]):
                kinds.append("current_mirror")
            if (at[output] == bt[common] or bt[output] == at[common]) and at[control] != bt[control]:
                kinds.append("cascode")
        for kind in kinds:
            names = [a["name"], b["name"]]
            detected.append({"id": "det_" + digest([kind, names])[:20], "kind": "matching",
                "source": "rule", "rationale": f"Conservative {kind} candidate: equal model and parameter tokens; requires review",
                "groups": [names], "style": "none", "topology": kind, "tolerance_nm": 0})
    return detected


def _inputs(run):
    cfg, project = layout_context(run)
    root = run.parent.parent
    link = read_json(local_path(run, "layout_inputs.json"))["schematic"]
    source = local_path(root, "runs/" + link["run_id"])
    net = read_json(local_path(source, "netlist_result.json"))
    raw = local_path(source, net["netlist"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != link["netlist_sha256"]:
        raise LayoutEvidenceError("Linked netlist changed")
    graph = topology(raw.decode("utf-8"), cfg.get("detection"))
    path = local_path(root, USER_PATH)
    if not path.is_relative_to(root / "private"):
        raise ValueError("User constraints must remain private")
    user_raw = path.read_bytes() if path.exists() else None
    user = json.loads(user_raw) if user_raw is not None else {"schema_version": 1, "constraints": []}
    user = validate_constraints(user, leading=cfg.get("allow_leading_digit", False))
    memory.validate_config(project)
    snapshot = memory.retrieve(project)
    memory_constraints, warnings = [], list(graph["warnings"])
    for entry in snapshot["entries"]:
        if entry["kind"] != "layout_constraints":
            continue
        try:
            document = json.loads(entry["summary"])
            for item in document["constraints"]:
                item["source"] = "memory:" + entry["id"]
            memory_constraints.extend(validate_constraints(document, leading=cfg.get("allow_leading_digit", False))["constraints"])
        except (ValueError, KeyError, TypeError):
            warnings.append(f"Reviewed memory {entry['id']} has no valid structured constraint summary; not inferred")
    merged = {c["id"]: c for c in detect_constraints(graph)}
    for item in memory_constraints:
        merged[item["id"]] = item
    for item in user["constraints"]:
        merged[item["id"]] = {**item, "source": "user"}
    body = validate_constraints({"schema_version": 1, "constraints": list(merged.values())}, leading=cfg.get("allow_leading_digit", False))
    provenance = {"layout_inputs_sha256": sha256(local_path(run, "layout_inputs.json")),
                  "netlist_sha256": link["netlist_sha256"], "hierarchy_sha256": digest(graph),
                  "user_sha256": hashlib.sha256(user_raw).hexdigest() if user_raw is not None else None,
                  "memory": snapshot}
    layout_context(run)
    if (path.read_bytes() if path.exists() else None) != user_raw or memory.retrieve(project) != snapshot:
        raise LayoutEvidenceError("Constraint inputs changed during retrieval")
    return cfg, {**body, "inputs": provenance, "warnings": sorted(set(warnings))}


def constraints_draft(run: Path):
    run = layout_path(run)
    with layout_lock(run):
        if status(run) not in {"LAYOUT_PREPARED", "CONSTRAINTS_DRAFTED"}:
            raise ValueError("Constraint drafting requires LAYOUT_PREPARED or CONSTRAINTS_DRAFTED")
        _, document = _inputs(run)
        path = local_path(run, "layout_constraints.draft.json")
        write_json(path, document)
        ref = {"path": path.name, "sha256": sha256(path)}
        if status(run) == "LAYOUT_PREPARED":
            transition(run, "CONSTRAINTS_DRAFTED", constraints_draft=ref)
        else:
            update_status(run, status(run), constraints_draft=ref)
        return {**ref, "status": status(run), "count": len(document["constraints"])}
