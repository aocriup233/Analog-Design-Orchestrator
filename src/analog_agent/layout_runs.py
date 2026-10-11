"""Separate, linked layout runs. No mutation of schematic run handoffs."""

from __future__ import annotations

import re
import os
import uuid
import json
import socket
import sys
import shutil
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

from .core import _fsync_directory, local_path, read_json, run_config, sha256, utc_now, write_json
from .layout_config import load_layout_config
from .state import status


class LayoutEvidenceError(ValueError):
    """Pinned evidence was changed or is unavailable; execution is unsafe."""


@dataclass
class LayoutSession:
    run: Path
    active: bool = True

    def require(self, run: Path) -> None:
        if not self.active or self.run != run.resolve():
            raise ValueError("A live lock-held layout session for this run is required")


def lock_info(run: Path) -> dict:
    run = layout_path(run)
    path = local_path(run, ".layout.lock")
    return {"locked": path.exists(), "owner": read_json(path) if path.exists() else None}


def layout_path(run: Path) -> Path:
    run = run.resolve(strict=True)
    if run.parent.name != "layouts" or not re.fullmatch(r"[A-Za-z0-9_-]+", run.name):
        raise ValueError("Expected a project layouts/RUN directory")
    local_path(run.parent.parent, str(run))
    task = read_json(local_path(run, "task.json"))
    if task.get("kind") != "layout" or task.get("run_id") != run.name:
        raise ValueError("Not a linked layout run")
    return run


@contextmanager
def layout_lock(run: Path):
    """One mutating command per run; a stale lock requires operator inspection."""
    path = local_path(run, ".layout.lock")
    try:
        stream = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError("Layout run is locked; reconcile the owning process before retry") from exc
    try:
        with stream:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                       "argv": sys.argv, "started_at": utc_now()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(run)
        session = LayoutSession(run.resolve())
        try:
            yield session
        finally:
            session.active = False
    finally:
        path.unlink()
        _fsync_directory(run)


def _schematic(root: Path, run_id: str) -> tuple[Path, dict, dict]:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid schematic run ID")
    runs = local_path(root, "runs")
    run = local_path(runs, run_id)
    if run.parent != runs or run.name != run_id:
        raise ValueError("Schematic run must be a direct child of runs/")
    if status(run) not in {"VERIFIED", "ANALYZED"}:
        raise ValueError("Layout requires a VERIFIED or ANALYZED schematic run")
    cfg = run_config(run)
    net = read_json(local_path(run, "netlist_result.json"))
    design = local_path(run, net["netlist"])
    if net.get("status") != "READY" or sha256(design) != net.get("netlist_sha256"):
        raise ValueError("Linked netlist handoff changed")
    simulation = read_json(local_path(run, "simulation_result.json"))
    if (simulation.get("status") != "DONE" or simulation.get("errors") != []
            or simulation.get("netlist_sha256") != net["netlist_sha256"]):
        raise ValueError("Linked simulation handoff does not verify the netlist")
    return run, cfg, net


def _spec_gate(source: Path, allow_failing: bool, reason: str) -> dict:
    path = local_path(source, "analysis_result.json")
    if not path.is_file():
        raise ValueError("Layout requires schematic analysis verdict PASS")
    verdict = read_json(path).get("status")
    if verdict != "PASS" and not (verdict == "FAIL" and status(source) == "ANALYZED" and
                                  allow_failing and isinstance(reason, str) and reason.strip()):
        raise ValueError("Layout requires PASS; ANALYZED FAIL needs --allow-failing-design --reason TEXT")
    if allow_failing and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("--allow-failing-design requires a nonempty reason")
    return {"verdict": verdict, "analysis_sha256": sha256(path),
            "allow_failing_design": allow_failing, "reason": reason.strip() if allow_failing else ""}


def _atomic_run(root: Path, inputs: dict, *, freeze=None, cloned_from=None) -> Path:
    layouts = local_path(root, "layouts")
    if layouts.parent != root or layouts.name != "layouts":
        raise ValueError("Layout storage must be the project's direct layouts/ directory")
    layouts.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    run = local_path(layouts, token)
    temporary = local_path(layouts, f".tmp-{token}")
    temporary.mkdir(exist_ok=False)
    try:
        now = utc_now()
        write_json(local_path(temporary, "layout_inputs.json"), inputs)
        task = {"run_id": run.name, "kind": "layout", "status": "LAYOUT_PREPARED", "created_at": now,
                "inputs_sha256": sha256(local_path(temporary, "layout_inputs.json")),
                "history": [{"status": "LAYOUT_PREPARED", "at": now}]}
        if cloned_from:
            task["cloned_from"] = cloned_from
        if freeze:
            # A carried review is evidence, not permission to bypass L2 gates.
            path = local_path(temporary, "floorplan_freezes/inherited.json")
            write_json(path, freeze)
            task["inherited_floorplan_freeze"] = {"path": "floorplan_freezes/inherited.json", "sha256": sha256(path)}
        write_json(local_path(temporary, "task.json"), task)
        _fsync_directory(temporary)
        _fsync_directory(layouts)
        if run.exists():
            raise FileExistsError(run)
        os.replace(temporary, run)
        _fsync_directory(layouts)
    except BaseException:
        # Only the exact task-created staging directory, never an existing run.
        if temporary.exists() and temporary.parent == layouts and temporary.name == f".tmp-{token}":
            shutil.rmtree(temporary)
            _fsync_directory(layouts)
        raise
    return run


def prepare_layout(schematic: Path, *, allow_failing_design: bool = False, reason: str = "") -> Path:
    schematic = schematic.resolve(strict=True)
    root = schematic.parent.parent
    if schematic.parent != local_path(root, "runs"):
        raise ValueError("Expected a schematic runs/RUN directory")
    source, _, net = _schematic(root, schematic.name)
    gate = _spec_gate(source, allow_failing_design, reason)
    _, manifest = load_layout_config(root)
    link = {"run_id": source.name, "netlist_sha256": net["netlist_sha256"],
            "netlist_result_sha256": sha256(local_path(source, "netlist_result.json")),
            "simulation_result_sha256": sha256(local_path(source, "simulation_result.json")),
            "config_sha256": sha256(local_path(source, "config.json"))}
    return _atomic_run(root, {"schematic": link, "private_sha256": manifest, "spec_gate": gate})


def _layout_context(run: Path) -> tuple[dict, dict]:
    """Revalidate all linked evidence and private inputs at each handoff."""
    run = layout_path(run)
    inputs_path = local_path(run, "layout_inputs.json")
    task = read_json(local_path(run, "task.json"))
    if sha256(inputs_path) != task.get("inputs_sha256"):
        raise ValueError("Layout input handoff changed")
    inputs = read_json(inputs_path)
    root = run.parent.parent
    source, project, net = _schematic(root, inputs["schematic"]["run_id"])
    gate = inputs.get("spec_gate", {})
    if _spec_gate(source, gate.get("allow_failing_design", False), gate.get("reason", "")) != gate:
        raise ValueError("Linked analysis evidence changed")
    link = inputs["schematic"]
    if (link["netlist_sha256"] != net["netlist_sha256"] or
            any(sha256(local_path(source, filename)) != link[key] for filename, key in (
                ("netlist_result.json", "netlist_result_sha256"),
                ("simulation_result.json", "simulation_result_sha256"), ("config.json", "config_sha256")))):
        raise ValueError("Linked schematic evidence changed")
    cfg, manifest = load_layout_config(root)
    if manifest != inputs["private_sha256"]:
        raise ValueError("Private layout inputs changed; create a new layout run")
    return cfg, project


def layout_context(run: Path) -> tuple[dict, dict]:
    try:
        return _layout_context(run)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise LayoutEvidenceError(str(exc)) from exc


def clone_layout(run: Path) -> Path:
    run = layout_path(run)
    # Read-only inspection does not acquire/change the original lock or artifacts.
    if lock_info(run)["locked"]:
        raise ValueError("Cannot clone an actively locked run; reconcile first")
    cfg, _ = layout_context(run)
    task_path = local_path(run, "task.json")
    original_hash = sha256(task_path)
    task = read_json(task_path)
    freeze = None
    if task.get("active_floorplan_freeze"):
        from .layout_floorplan import _frozen
        freeze = _frozen(run, cfg)
    elif task.get("inherited_floorplan_freeze"):
        ref = task["inherited_floorplan_freeze"]
        path = local_path(run, ref["path"])
        if sha256(path) != ref["sha256"]:
            raise LayoutEvidenceError("Inherited freeze receipt changed")
        freeze = read_json(path)
    inputs = read_json(local_path(run, "layout_inputs.json"))
    layout_context(run)
    if sha256(task_path) != original_hash or lock_info(run)["locked"]:
        raise LayoutEvidenceError("Clone source changed during inspection")
    return _atomic_run(run.parent.parent, inputs, freeze=freeze,
                       cloned_from={"run_id": run.name, "task_sha256": original_hash})
