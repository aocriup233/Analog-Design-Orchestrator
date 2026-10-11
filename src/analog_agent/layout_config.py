"""Private layout configuration; explicit exceptions for external rule directories."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .core import local_path
from .parameters import number_value, validate_value

CONFIG_PATH = "private/layout/config.json"


def identifier(value, label: str, *, allow_leading_digit: bool = False) -> str:
    initial = r"[A-Za-z0-9_]" if allow_leading_digit else r"[A-Za-z_]"
    if not isinstance(value, str) or not re.fullmatch(initial + r"[A-Za-z0-9_-]*", value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return validate_value(label, value)


def validate_identifiers(items, *, allow_leading_digit=False) -> None:
    errors = []
    for label, value in items:
        try:
            identifier(value, label, allow_leading_digit=allow_leading_digit)
        except ValueError:
            errors.append(f"{label}={value!r}")
    if errors:
        raise ValueError("Offending names: " + ", ".join(errors))


def stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def external_path(root: Path, value, *, directory: bool) -> Path:
    """Never weaken local_path: external paths are permitted only by this policy."""
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("Layout rule/run paths must be user-specified absolute paths")
    path = Path(value).resolve(strict=True)
    repository = Path(__file__).resolve().parents[2]
    forbidden = [root.resolve()]
    if (repository / "pyproject.toml").is_file():
        forbidden.append(repository)
    if any(path.is_relative_to(base) for base in forbidden):
        raise ValueError("Layout rule/run paths must be outside the project and ADO repository")
    if directory:
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            raise ValueError("Layout directory must exist and be readable/searchable")
        # Opening the directory checks access without reading any rule content.
        with os.scandir(path) as entries:
            next(entries, None)
    else:
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError("Layout runset must exist and be readable")
        with path.open("rb"):
            pass
    return path


def load_layout_config(root: Path) -> tuple[dict, dict[str, str]]:
    """Read exactly the private bytes whose digest is returned; never publish them."""
    root = root.resolve()
    path = local_path(root, CONFIG_PATH)
    # A symlink into public config must not turn this into a public config hook.
    if not path.is_relative_to(root / "private"):
        raise ValueError("Layout config must remain inside private/")
    data = path.read_bytes()
    document = json.loads(data)
    if not isinstance(document, dict) or set(document) != {"layout"}:
        raise ValueError("Private layout config must contain only a layout object")
    cfg = document["layout"]
    if not isinstance(cfg, dict) or set(cfg) - {"tool", "library", "cell", "timeout_s", "drc", "lvs", "bridge", "allow_leading_digit"}:
        raise ValueError("Unknown layout configuration fields")
    if not isinstance(cfg.get("tool"), str) or cfg["tool"] not in {"pegasus", "calibre"}:
        raise ValueError("layout.tool must be pegasus or calibre")
    leading = cfg.get("allow_leading_digit", False)
    if type(leading) is not bool:
        raise ValueError("layout.allow_leading_digit must be boolean")
    validate_identifiers([(f"layout.{name}", cfg.get(name)) for name in ("library", "cell")],
                         allow_leading_digit=leading)
    timeout = number_value(validate_value("layout.timeout_s", cfg.get("timeout_s", 300), {"type": "number"}))
    if timeout <= 0:
        raise ValueError("layout.timeout_s must be positive")
    directories = {}
    for stage in ("drc", "lvs"):
        section = cfg.get(stage)
        if not isinstance(section, dict) or set(section) - {"rule_dir", "run_dir", "runset"}:
            raise ValueError(f"layout.{stage} requires rule_dir and run_dir")
        for key in ("rule_dir", "run_dir"):
            directories[stage, key] = external_path(root, section.get(key), directory=True)
        if not os.access(directories[stage, "run_dir"], os.W_OK):
            raise ValueError(f"layout.{stage}.run_dir must be writable")
        if "runset" in section:
            external_path(root, section["runset"], directory=False)
    for run_stage in ("drc", "lvs"):
        for rule_stage in ("drc", "lvs"):
            work, rules = directories[run_stage, "run_dir"], directories[rule_stage, "rule_dir"]
            if work.is_relative_to(rules) or rules.is_relative_to(work):
                raise ValueError(f"layout.{run_stage}.run_dir overlaps layout.{rule_stage}.rule_dir")
    bridge = cfg.get("bridge", {})
    if not isinstance(bridge, dict) or set(bridge) - {"reader"}:
        raise ValueError("layout.bridge permits only reader")
    manifest = {CONFIG_PATH: hashlib.sha256(data).hexdigest()}
    reader = bridge.get("reader")
    if reader is not None:
        if not isinstance(reader, str) or ":" not in reader:
            raise ValueError("layout.bridge.reader must be a function reference")
        location, _ = reader.rsplit(":", 1)
        if location.endswith(".py"):
            plugin = local_path(root, location)
            manifest[plugin.relative_to(root).as_posix()] = stream_sha256(plugin)
        # Module allowlisting is enforced by plugins.load_function at execution.
    return cfg, manifest


def verification_inputs(root: Path, cfg: dict, stage: str, used_files: list[str]) -> dict:
    """Hash only files the verification worker actually consumes (L3 hook).

    Does not guess dependencies by recursively copying or hashing a rule tree.
    The future tool adapter must report transitive rule inputs, not just a runset.
    """
    if stage not in {"drc", "lvs"} or not isinstance(used_files, list) or not used_files:
        raise ValueError("Verification requires a stage and explicit used rule files")
    section = cfg[stage]
    rule_dir = external_path(root, section["rule_dir"], directory=True)
    runset = external_path(root, section["runset"], directory=False) if "runset" in section else None
    hashes = {}
    for value in used_files:
        path = external_path(root, value, directory=False)
        if not path.is_relative_to(rule_dir) and path != runset:
            raise ValueError("Used verification input is outside the configured rule directory/runset")
        hashes[str(path)] = stream_sha256(path)
    if runset is not None and str(runset) not in hashes:
        raise ValueError("Configured runset must be included in used verification inputs")
    return {"stage": stage, "tool": cfg["tool"], "rule_sha256": hashes}
