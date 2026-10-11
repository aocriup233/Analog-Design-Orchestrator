"""Separate, linked layout runs. No mutation of schematic run handoffs."""

from __future__ import annotations

import re
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import _fsync_directory, local_path, read_json, run_config, sha256, utc_now, write_json
from .layout_config import load_layout_config
from .state import status


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
            stream.write(utc_now())
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(run)
        yield
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


def prepare_layout(schematic: Path) -> Path:
    schematic = schematic.resolve(strict=True)
    root = schematic.parent.parent
    if schematic.parent != local_path(root, "runs"):
        raise ValueError("Expected a schematic runs/RUN directory")
    source, _, net = _schematic(root, schematic.name)
    _, manifest = load_layout_config(root)
    layouts = local_path(root, "layouts")
    if layouts.parent != root or layouts.name != "layouts":
        raise ValueError("Layout storage must be the project's direct layouts/ directory")
    run = local_path(layouts, uuid.uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    now = utc_now()
    link = {"run_id": source.name, "netlist_sha256": net["netlist_sha256"],
            "netlist_result_sha256": sha256(local_path(source, "netlist_result.json")),
            "simulation_result_sha256": sha256(local_path(source, "simulation_result.json")),
            "config_sha256": sha256(local_path(source, "config.json"))}
    write_json(local_path(run, "layout_inputs.json"), {"schematic": link, "private_sha256": manifest})
    write_json(local_path(run, "task.json"), {"run_id": run.name, "kind": "layout",
               "status": "LAYOUT_PREPARED", "created_at": now,
               "inputs_sha256": sha256(local_path(run, "layout_inputs.json")),
               "history": [{"status": "LAYOUT_PREPARED", "at": now}]})
    return run


def layout_context(run: Path) -> tuple[dict, dict]:
    """Revalidate all linked evidence and private inputs at each handoff."""
    run = layout_path(run)
    inputs_path = local_path(run, "layout_inputs.json")
    task = read_json(local_path(run, "task.json"))
    if sha256(inputs_path) != task.get("inputs_sha256"):
        raise ValueError("Layout input handoff changed")
    inputs = read_json(inputs_path)
    root = run.parent.parent
    source, project, net = _schematic(root, inputs["schematic"]["run_id"])
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
