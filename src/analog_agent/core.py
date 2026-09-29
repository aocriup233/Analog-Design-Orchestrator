"""Configuration, run storage, and strict local path handling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRIVATE_OVERRIDES = (
    "private/global/config/overrides.json",
    "private/agents/netlist/config/overrides.json",
    "private/agents/simulation/config/overrides.json",
    "private/agents/analysis/config/overrides.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_path(root: Path, relative: str) -> Path:
    """Resolve a configured file below the project root, with no traversal."""
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes project directory: {relative}")
    return candidate


def _merge(target: dict, overlay: dict) -> None:
    for key, value in overlay.items():
        if key.startswith("_"):
            raise ValueError(f"Private override cannot set internal key: {key}")
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def private_manifest(root: Path) -> dict[str, str]:
    return {relative: sha256(root / relative) for relative in PRIVATE_OVERRIDES
            if (root / relative).is_file()}


def _apply_private(cfg: dict[str, Any], expected: dict[str, str] | None = None) -> dict[str, Any]:
    root = Path(cfg["_project_root"])
    current = private_manifest(root)
    if expected is not None and current != expected:
        raise ValueError("Private configuration changed since this run was created")
    for relative in PRIVATE_OVERRIDES:
        if relative in current:
            _merge(cfg, read_json(root / relative))
    return cfg


def _validate_config(cfg: dict[str, Any]) -> None:
    root = Path(cfg["_project_root"])
    netlist = cfg.get("netlist", {})
    split = "block" in netlist or "testbenches" in netlist
    if split:
        if cfg.get("template") or netlist.get("generator"):
            raise ValueError("Split block/testbench mode cannot use legacy template/generator")
        block, benches = netlist.get("block"), netlist.get("testbenches")
        if not isinstance(block, dict) or not isinstance(benches, dict) or not benches:
            raise ValueError("netlist.block and nonempty netlist.testbenches are required")
        if block.get("kind") not in {"source", "canvas"}:
            raise ValueError("netlist.block.kind must be source or canvas")
        if not isinstance(block.get("name"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", block["name"]):
            raise ValueError("netlist.block.name must be a Spectre identifier")
        pins = block.get("pins")
        if not isinstance(pins, list) or not pins or any(
                not isinstance(pin, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pin)
                for pin in pins) or len(set(pin.lower() for pin in pins)) != len(pins):
            raise ValueError("netlist.block.pins must be unique explicit port names")
        source_key = "path" if block["kind"] == "source" else "project"
        if not isinstance(block.get(source_key), str) or not local_path(root, block[source_key]).is_file():
            raise ValueError(f"netlist.block.{source_key} must name a project file")
        if block["kind"] == "canvas" and not block.get("adapter") and not block.get("canvas_root"):
            raise ValueError("Canvas block needs a private mapping adapter or canvas_root")
        dependencies = block.get("dependencies", [])
        if not isinstance(dependencies, list) or any(
                not isinstance(item, str) or not local_path(root, item).is_file()
                for item in dependencies):
            raise ValueError("netlist.block.dependencies must name project files")
        default = netlist.get("default_testbench")
        if default not in benches:
            raise ValueError("netlist.default_testbench must select a declared testbench")
        for name, definition in benches.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
                raise ValueError(f"Invalid testbench name: {name}")
            if not isinstance(definition, dict) or ("path" in definition) == ("generator" in definition):
                raise ValueError(f"Testbench {name} needs exactly one path or generator")
            if "path" in definition and not local_path(root, definition["path"]).is_file():
                raise FileNotFoundError(definition["path"])
    elif not (cfg.get("template") or netlist.get("generator")):
        raise ValueError("Configuration needs template or netlist.generator")
    if not isinstance(cfg.get("parameters", {}), dict):
        raise ValueError("parameters must be an object")
    if cfg.get("workflow", {}).get("submission", "manual") not in {"manual", "auto"}:
        raise ValueError("workflow.submission must be manual or auto")
    if cfg.get("workflow", {}).get("return_mode", "summary") not in {"summary", "full"}:
        raise ValueError("workflow.return_mode must be summary or full")
    if cfg.get("workflow", {}).get("cleanup", "never") not in {"never", "after_success"}:
        raise ValueError("workflow.cleanup must be never or after_success")
    if cfg.get("simulation", {}).get("backend", "bridge") not in {"bridge", "local", "lsf", "external"}:
        raise ValueError("simulation.backend must be bridge, local, lsf, or external")
    if cfg.get("simulation", {}).get("backend") == "lsf" and not cfg.get("simulation", {}).get("adapter"):
        raise ValueError("simulation.adapter is required for the lsf backend")
    if cfg.get("simulation", {}).get("remote_cleanup", "never") not in {"never", "manual", "after_verified"}:
        raise ValueError("simulation.remote_cleanup must be never, manual, or after_verified")
    if cfg.get("simulation", {}).get("output_format", "psfascii") != "psfascii":
        raise ValueError("Only psfascii output is supported by the bridge parser")
    if not cfg.get("metrics") or not cfg.get("rules"):
        raise ValueError("Configuration needs metrics and rules")
    if cfg.get("template"):
        template = local_path(root, cfg["template"])
        if not template.is_file():
            raise FileNotFoundError(template)
    for name in cfg.get("parameters", {}):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid parameter name: {name}")
    for item in cfg.get("simulation", {}).get("include_files", []):
        if not local_path(root, item).is_file():
            raise FileNotFoundError(item)
    for name, paths in cfg.get("simulation", {}).get("rules_by_testbench", {}).items():
        if name not in netlist.get("testbenches", {}) or not isinstance(paths, list) or not paths:
            raise ValueError(f"Invalid simulation rule set for testbench: {name}")
        for item in paths:
            if not isinstance(item, str) or not local_path(root, item).is_file():
                raise FileNotFoundError(item)
    from .memory import validate_config as validate_memory
    validate_memory(cfg)


def load_config(path: Path) -> dict[str, Any]:
    path = path.resolve()
    cfg = read_json(path)
    cfg["_project_root"] = str(path.parent)
    cfg["_config_path"] = str(path)
    _apply_private(cfg)
    _validate_config(cfg)
    return cfg


def create_run(config_path: Path, iteration: int, overrides: dict[str, str] | None = None,
               testbench: str | None = None) -> Path:
    runtime = load_config(config_path)
    maximum = int(runtime.get("workflow", {}).get("max_iterations", 1))
    if not 0 <= iteration < maximum:
        raise ValueError(f"iteration must be in [0, {maximum - 1}]")
    root = Path(runtime["_project_root"])
    if testbench is not None and testbench not in runtime.get("netlist", {}).get("testbenches", {}):
        raise ValueError(f"Unknown testbench: {testbench}")
    run = root / "runs" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"
    run.mkdir(parents=True, exist_ok=False)
    public = read_json(config_path)
    public["_project_root"] = str(root)
    public["_config_path"] = str(config_path.resolve())
    write_json(run / "config.json", public)
    write_json(run / "private_manifest.json", private_manifest(root))
    if runtime.get("memory"):
        from .memory import retrieve
        write_json(run / "memory_snapshot.json", retrieve(runtime))
    write_json(run / "task.json", {
        "run_id": run.name, "iteration": iteration,
        "overrides": overrides or {}, "testbench": testbench,
        "created_at": utc_now(), "status": "CREATED",
        "history": [{"status": "CREATED", "at": utc_now()}],
    })
    return run


def run_config(run: Path) -> dict[str, Any]:
    cfg = read_json(run / "config.json")
    manifest_path = run / "private_manifest.json"
    expected = read_json(manifest_path) if manifest_path.is_file() else {}
    _apply_private(cfg, expected)
    _validate_config(cfg)
    return cfg


def update_status(run: Path, status: str, **fields: Any) -> None:
    task = read_json(run / "task.json")
    task.update(status=status, updated_at=utc_now(), **fields)
    write_json(run / "task.json", task)
