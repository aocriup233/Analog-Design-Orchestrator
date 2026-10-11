"""Bind generated CDL to immutable simulation bytes, not the current schematic."""

from __future__ import annotations

import os
import uuid

from .core import local_path, read_json, sha256
from .layout_constraints import digest, _pinned
from .layout_executor import execute, configured
from .layout_runs import LayoutEvidenceError
from .layout_routing import fields
from .layout_worker import publish
from .parameters import validate_value, number_value


def canonical_circuit(document):
    """Strict normalized electrical identity; no lossy topology detector reuse."""
    fields(document, {"schema_version", "top", "ports", "devices", "complete", "unparsed", "input_sha256"})
    if document["schema_version"] != 1 or document["complete"] is not True or document["unparsed"] != []:
        raise ValueError("Complete netlist parsing required; unknown/skipped constructs cannot establish equivalence")
    if not isinstance(document["top"], str) or not document["top"]:
        raise ValueError("Canonical top must be explicit")
    ports = document["ports"]
    if not isinstance(ports, list) or not all(isinstance(n, str) and n for n in ports) or len(ports) != len(set(ports)):
        raise ValueError("Canonical ports must be distinct and ordered")
    if not isinstance(document["devices"], list) or len(document["devices"]) > 1000000:
        raise ValueError("Canonical devices must be a bounded array")
    devices, seen = [], set()
    for original in document["devices"]:
        fields(original, {"id", "model", "terminals", "parameters"})
        if (not all(isinstance(original[k], str) and original[k] for k in ("id", "model")) or original["id"] in seen
                or not isinstance(original["terminals"], dict) or not original["terminals"]
                or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in original["terminals"].items())
                or not isinstance(original["parameters"], dict)):
            raise ValueError("Incomplete device, duplicate ID or invalid connectivity")
        seen.add(original["id"])
        parameters = {}
        for name, value in original["parameters"].items():
            if not isinstance(name, str) or not name:
                raise ValueError("Parameter names must be explicit")
            fields(value, {"type", "value"})
            if value["type"] == "number":
                atom = validate_value(name, value["value"], {"type": "number"})
                parameters[name] = {"type": "number", "value": format(number_value(atom), ".17g")}
            elif value["type"] == "enum":
                parameters[name] = {"type": "enum", "value": validate_value(name, value["value"])}
            else:
                raise ValueError("Canonical parameters must be resolved numeric or enum values, not expressions")
        devices.append({"id": original["id"], "model": original["model"], "terminals": original["terminals"], "parameters": parameters})
    return {"schema_version": 1, "top": document["top"], "ports": ports, "devices": sorted(devices, key=lambda d: d["id"])}


def prepare_source(run, cfg, *, executor=None):
    executor = executor or execute
    configured(run, "generate_lvs_source")
    configured(run, "normalize_lvs_source")
    link = read_json(local_path(run, "layout_inputs.json"))["schematic"]
    source = local_path(run.parent.parent, f"runs/{link['run_id']}")
    net = read_json(local_path(source, "netlist_result.json"))
    original = local_path(source, net["netlist"])
    raw = original.read_bytes()
    import hashlib
    if hashlib.sha256(raw).hexdigest() != link["netlist_sha256"]:
        raise LayoutEvidenceError("Simulation design netlist changed before CDL generation")
    directory = local_path(run, f"lvs_sources/{uuid.uuid4().hex}")
    directory.mkdir(parents=True, exist_ok=False)
    # Exact immutable simulator design bytes; never request a current GUI export.
    frozen = directory / "simulation.scs"
    with frozen.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    request = {"input": frozen.relative_to(run).as_posix(), "input_sha256": link["netlist_sha256"],
               "destination": (directory / "source.cdl").relative_to(run).as_posix(), "top": cfg["lvs_source"]["top"]}
    generated = executor(run, "generate_lvs_source", request)
    fields(generated, {"path", "sha256", "input_sha256", "generator_version"})
    cdl = local_path(directory, generated["path"])
    if (cdl != directory / "source.cdl" or not cdl.is_file() or not cdl.stat().st_size
            or sha256(cdl) != generated["sha256"] or generated["input_sha256"] != link["netlist_sha256"]
            or not isinstance(generated["generator_version"], str) or not generated["generator_version"]):
        raise LayoutEvidenceError("CDL generation does not bind the immutable source bytes")
    canonical = []
    for path, kind in ((frozen, "spectre"), (cdl, "cdl")):
        hashed = sha256(path)
        value = executor(run, "normalize_lvs_source", {"path": path.relative_to(run).as_posix(),
                        "input_sha256": hashed, "format": kind, "top": request["top"]})
        if value.get("input_sha256") != hashed or sha256(path) != hashed:
            raise LayoutEvidenceError("Netlist normalization did not bind the actual parsed bytes")
        canonical.append(canonical_circuit(value))
    if canonical[0]["top"] != request["top"] or canonical[0] != canonical[1]:
        raise LayoutEvidenceError("CDL devices/connectivity/parameters differ from the pinned simulation design")
    if sha256(frozen) != link["netlist_sha256"] or sha256(original) != link["netlist_sha256"] or sha256(cdl) != generated["sha256"]:
        raise LayoutEvidenceError("LVS source bytes changed during generation/comparison")
    receipt = {"schema_version": 1, "schematic": link, "simulation_copy": frozen.relative_to(run).as_posix(),
               "simulation_sha256": link["netlist_sha256"], "cdl": cdl.relative_to(run).as_posix(),
               "cdl_sha256": generated["sha256"], "canonical_sha256": digest(canonical[0]),
               "canonical": canonical[0], "generator_version": generated["generator_version"]}
    return publish(run, "lvs_source_receipts", receipt)


def check_source(run, reference):
    receipt = _pinned(run, reference)
    link = read_json(local_path(run, "layout_inputs.json"))["schematic"]
    if (receipt["schematic"] != link or receipt["simulation_sha256"] != link["netlist_sha256"]
            or sha256(local_path(run, receipt["simulation_copy"])) != link["netlist_sha256"]
            or sha256(local_path(run, receipt["cdl"])) != receipt["cdl_sha256"]
            or digest(receipt["canonical"]) != receipt["canonical_sha256"]):
        raise LayoutEvidenceError("Pinned LVS source proof/artifacts changed")
    return receipt
