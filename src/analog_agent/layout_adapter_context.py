"""Versioned, run-confined path contract for audited layout adapters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .core import local_path
from .layout_runs import layout_path


def adapter_context(run: Path, request_dir: Path, result_dir: Path) -> dict:
    run = layout_path(run)
    request_dir = local_path(run, str(request_dir))
    result_dir = local_path(run, str(result_dir))
    return {"schema_version": 1, "run_root": str(run),
            "request_dir": str(request_dir), "result_dir": str(result_dir)}


def validate_adapter_context(value: dict, run: Path, request_dir: Path, result_dir: Path) -> dict:
    expected = adapter_context(run, request_dir, result_dir)
    if not isinstance(value, dict) or value != expected:
        raise ValueError("Layout adapter context is missing, unversioned, or inconsistent with the run/channel")
    return expected


def context_sha256(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def resolve_run_path(context: dict, relative: str) -> Path:
    """Resolve an adapter payload path against its validated layout run, never cwd."""
    if not isinstance(context, dict) or set(context) != {"schema_version", "run_root", "request_dir", "result_dir"}:
        raise ValueError("Invalid layout adapter context")
    run = layout_path(Path(context["run_root"]))
    request_dir = local_path(run, context["request_dir"])
    result_dir = local_path(run, context["result_dir"])
    validate_adapter_context(context, run, request_dir, result_dir)
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or ":" in relative or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("Adapter path must be a nonempty run-relative path without traversal")
    path = local_path(run, relative)
    if path == run:
        raise ValueError("Adapter path cannot be the run root")
    return path
