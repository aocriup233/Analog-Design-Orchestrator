"""DRC/LVS evidence chain. Native launch/parsing is an audited private adapter."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .core import _fsync_directory, local_path, read_json, sha256, update_status, write_json
from .layout_config import external_path, verification_inputs
from .layout_constraints import _pinned, digest
from .layout_executor import configured, execute
from .layout_floorplan import require_unchanged_floorplan
from .layout_runs import LayoutEvidenceError, layout_context, layout_lock, layout_path
from .layout_routing import fields
from .layout_worker import attention, publish, snapshot
from .state import status, transition


def parse_report(raw: bytes, *, stage, tool):
    """Strict portable normalization format, NOT a guessed native tool grammar."""
    data = json.loads(raw)
    fields(data, {"schema_version", "stage", "tool", "completed", "counts_by_rule", "violations", "lvs_match"})
    if data["schema_version"] != 1 or data["stage"] != stage or data["tool"] != tool or data["completed"] is not True:
        raise ValueError("Verification report is incomplete or for another stage/tool")
    counts = data["counts_by_rule"]
    if (not isinstance(counts, dict) or not all(isinstance(k, str) and k for k in counts)
            or not all(type(v) is int and v >= 0 for v in counts.values())):
        raise ValueError("Invalid rule counts")
    violations = data["violations"]
    if not isinstance(violations, list) or len(violations) > 100000:
        raise ValueError("Violation list must be bounded")
    actual, ids = {}, set()
    for v in violations:
        fields(v, {"id", "rule", "layer", "bbox", "message", "nets"})
        if (not all(isinstance(v[k], str) and v[k] for k in ("id", "rule", "layer", "message"))
                or v["id"] in ids or not isinstance(v["nets"], list) or not all(isinstance(n, str) and n for n in v["nets"])):
            raise ValueError("Invalid violation identity")
        ids.add(v["id"])
        box = v["bbox"]
        if (not isinstance(box, list) or len(box) != 2 or any(not isinstance(p, list) or len(p) != 2 for p in box)
                or any(type(x) is not int for p in box for x in p) or any(box[0][i] > box[1][i] for i in (0, 1))):
            raise ValueError("Violation bbox must be ordered integer nm")
        actual[v["rule"]] = actual.get(v["rule"], 0) + 1
    if any(counts.get(rule) != count for rule, count in actual.items()) or sum(counts.values()) != len(violations):
        raise ValueError("Violation list and rule counts disagree; truncated reports cannot certify clean")
    if (stage == "lvs" and type(data["lvs_match"]) is not bool) or (stage == "drc" and data["lvs_match"] is not None):
        raise ValueError("LVS needs explicit match; DRC must not claim an LVS result")
    return {"counts_by_rule": counts, "violations": violations,
            "clean": not violations and (stage == "drc" or data["lvs_match"] is True)}


def _workdir(run, cfg, stage):
    root = external_path(run.parent.parent, cfg[stage]["run_dir"], directory=True)
    work = root / run.name
    task = read_json(local_path(run, "task.json"))
    reference = task.get("verification_workdirs")
    record = _pinned(run, reference) if reference else {}
    # DRC/LVS may share a base; only an existing, hash-pinned ownership record
    # from this run permits reuse. Otherwise exclusive creation, never exist_ok.
    key = str(work)
    if key not in record:
        work.mkdir(exist_ok=False)
        marker = work / ".ado-owner.json"
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"run_id": run.name, "inputs_sha256": sha256(local_path(run, "layout_inputs.json"))}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        record[key] = sha256(marker)
        update_status(run, status(run), verification_workdirs=publish(run, "verification_workdir_receipts", record))
        _fsync_directory(work)
    if work.resolve(strict=True) != work or sha256(work / ".ado-owner.json") != record[key]:
        raise LayoutEvidenceError("Verification work directory ownership changed")
    destination = work / f"{stage}-{uuid.uuid4().hex}"
    destination.mkdir(exist_ok=False)
    return destination


def _export(run, cellhash):
    destination = local_path(run, f"verification_snapshots/{uuid.uuid4().hex}")
    destination.mkdir(parents=True, exist_ok=False)
    result = execute(run, "export_verification_snapshot", {"cellview_sha256": cellhash,
                     "destination": destination.relative_to(run).as_posix()})
    fields(result, {"cellview_sha256", "files"})
    if result["cellview_sha256"] != cellhash or not isinstance(result["files"], dict) or not result["files"]:
        raise LayoutEvidenceError("Verification export does not bind the saved cellview")
    for relative, expected in result["files"].items():
        path = local_path(destination, relative)
        if not path.is_file() or sha256(path) != expected:
            raise LayoutEvidenceError("Verification export artifact/hash mismatch")
    return {"directory": destination.relative_to(run).as_posix(), **result}


def _unchanged_export(run, export):
    directory = local_path(run, export["directory"])
    for relative, expected in export["files"].items():
        if sha256(local_path(directory, relative)) != expected:
            raise LayoutEvidenceError("Immutable verification snapshot changed")


def verify(run: Path, *, session=None):
    run = layout_path(run)
    if session is None:
        with layout_lock(run) as held:
            return verify(run, session=held)
    session.require(run)
    if status(run) not in {"ROUTED", "DRC_DONE", "LVS_DONE"}:
        raise ValueError("Verification requires a routed run or a verified stage handoff")
    executed = False
    try:
        cfg, _ = layout_context(run)
        configured(run, "verify")
        require_unchanged_floorplan(run, "verification", session=session)
        task = read_json(local_path(run, "task.json"))
        routing = _pinned(run, task["active_routing_result"])
        review_ref = routing["inputs"]["routing_review"]
        review = _pinned(run, review_ref)
        if (task.get("active_routing_review") != review_ref or review["floorplan_freeze"] != task["active_floorplan_freeze"]
                or review["constraints_freeze"] != task["active_constraints_freeze"]):
            raise LayoutEvidenceError("Routing human review no longer binds the current frozen inputs")
        historical = _pinned(run, routing["snapshot"])
        if historical["sha256"] != routing["cellview_sha256"]:
            raise LayoutEvidenceError("Pinned routed snapshot identity differs from its result")
        full = snapshot(run, cfg)
        if full["sha256"] != routing["cellview_sha256"]:
            raise LayoutEvidenceError("Cellview changed since routing")
        if routing["unrouted_nets"]:
            raise ValueError("Unrouted nets remain; layout cannot be certified")
        export = _export(run, full["sha256"]) if status(run) == "ROUTED" else _pinned(run, task["active_drc_report"])["export"]
        _unchanged_export(run, export)
        if status(run) == "LVS_DONE" and not _pinned(run, task["active_lvs_report"])["clean"]:
            return {"status": status(run), "clean": False, "failed_stage": "lvs", "report": task["active_lvs_report"]}
        for stage, start, end in (("drc", "ROUTED", "DRC_DONE"), ("lvs", "DRC_DONE", "LVS_DONE")):
            if status(run) != start:
                continue
            if stage == "lvs":
                drc = _pinned(run, read_json(local_path(run, "task.json"))["active_drc_report"])
                if not drc["clean"]:
                    return {"status": status(run), "clean": False, "failed_stage": "drc"}
                if verification_inputs(run.parent.parent, cfg, "drc", list(drc["rule_sha256"]))["rule_sha256"] != drc["rule_sha256"]:
                    raise LayoutEvidenceError("DRC rules changed before LVS resume")
            section = cfg[stage]
            declared = execute(run, "verify", {"phase": "inputs", "stage": stage, "tool": cfg["tool"], "config": section})
            fields(declared, {"used_files", "version"})
            if not isinstance(declared["version"], str) or not declared["version"].strip():
                raise ValueError("Verification adapter must record a tool version")
            rules = verification_inputs(run.parent.parent, cfg, stage, declared["used_files"])
            work = _workdir(run, cfg, stage)
            output = local_path(run, f"verification_raw/{stage}-{uuid.uuid4().hex}")
            output.mkdir(parents=True, exist_ok=False)
            executed = True
            receipt = execute(run, "verify", {"phase": "run", "stage": stage, "tool": cfg["tool"],
                "config": section, "work_dir": str(work), "output_dir": output.relative_to(run).as_posix(),
                "export": export, "schematic": read_json(local_path(run, "layout_inputs.json"))["schematic"], "rules": rules})
            fields(receipt, {"tool", "version", "exit_code", "cellview_sha256", "used_files", "report", "raw_reports"})
            if (receipt["tool"] != cfg["tool"] or receipt["version"] != declared["version"] or type(receipt["exit_code"]) is not int
                    or receipt["exit_code"] != 0 or receipt["cellview_sha256"] != full["sha256"]
                    or verification_inputs(run.parent.parent, cfg, stage, receipt["used_files"]) != rules):
                raise LayoutEvidenceError("Tool exit, version, cellview or consumed rule hashes mismatch")
            if not isinstance(receipt["raw_reports"], dict) or not receipt["raw_reports"]:
                raise LayoutEvidenceError("Native raw reports and their hashes are required")
            hashes = {}
            for relative, expected in receipt["raw_reports"].items():
                path = local_path(output, relative)
                if not path.is_file() or sha256(path) != expected:
                    raise LayoutEvidenceError("Raw verification report/hash mismatch")
                hashes[path.relative_to(run).as_posix()] = expected
            normalized = local_path(output, receipt["report"])
            raw = normalized.read_bytes()
            import hashlib
            if hashes.get(normalized.relative_to(run).as_posix()) != hashlib.sha256(raw).hexdigest():
                raise LayoutEvidenceError("Normalized report must be included in raw hashes")
            parsed = parse_report(raw, stage=stage, tool=cfg["tool"])
            _unchanged_export(run, export)
            require_unchanged_floorplan(run, "verification", session=session)
            if snapshot(run, cfg)["sha256"] != full["sha256"]:
                raise LayoutEvidenceError("Cellview changed during verification")
            report = {"schema_version": 1, "stage": stage, "tool": cfg["tool"], "version": declared["version"],
                **rules, **parsed, "cellview_sha256": full["sha256"], "placement_sha256": full["placement_sha256"],
                "routing_result": task["active_routing_result"], "export": export, "raw_reports": hashes,
                "work_dir": str(work)}
            reference = publish(run, f"{stage}_reports", report)
            write_json(local_path(run, f"{stage}_report.json"), report)
            transition(run, end, **{f"active_{stage}_report": reference})
            if not report["clean"]:
                return {"status": status(run), "clean": False, "failed_stage": stage, "report": reference}
        current = read_json(local_path(run, "task.json"))
        drc, lvs = (_pinned(run, current[f"active_{s}_report"]) for s in ("drc", "lvs"))
        for report in (drc, lvs):
            if (not report["clean"] or report["cellview_sha256"] != full["sha256"] or report["placement_sha256"] != full["placement_sha256"]
                    or report["routing_result"] != current["active_routing_result"] or report["export"] != export):
                raise LayoutEvidenceError("DRC/LVS do not certify the same routed saved snapshot")
            if verification_inputs(run.parent.parent, cfg, report["stage"], list(report["rule_sha256"]))["rule_sha256"] != report["rule_sha256"]:
                raise LayoutEvidenceError("Rule inputs changed since verification")
            for relative, expected in report["raw_reports"].items():
                if sha256(local_path(run, relative)) != expected:
                    raise LayoutEvidenceError("Verified raw report changed")
        require_unchanged_floorplan(run, "verification", session=session)
        if snapshot(run, cfg)["sha256"] != full["sha256"]:
            raise LayoutEvidenceError("Final saved cellview differs from the verified snapshot")
        ref = publish(run, "layout_verifications", {"drc": current["active_drc_report"], "lvs": current["active_lvs_report"],
                      "routing_result": current["active_routing_result"], "cellview_sha256": full["sha256"],
                      "placement_sha256": full["placement_sha256"], "routing_review": routing["inputs"]["routing_review"]})
        transition(run, "LAYOUT_VERIFIED", layout_verification=ref)
        return {**ref, "status": status(run), "clean": True}
    except Exception as exc:
        if executed or isinstance(exc, (LayoutEvidenceError, TimeoutError, RuntimeError, OSError)):
            attention(run, type(exc).__name__)
        raise
