"""Netlist worker: render an immutable, hashed Spectre input from a template."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .core import local_path, read_json, run_config, sha256, utc_now, write_json
from .plugins import load_function
from .state import transition


def prepare(run: Path) -> dict:
    run = run.resolve()
    if (run / "netlist_result.json").exists():
        raise ValueError("Netlist already prepared; create a new run for a new revision")
    cfg = run_config(run)
    task = read_json(run / "task.json")
    iteration = int(task["iteration"])
    values: dict[str, str] = {}
    for name, definition in cfg.get("parameters", {}).items():
        if "candidates" in definition and iteration < len(definition["candidates"]):
            value = definition["candidates"][iteration]
        else:
            value = definition["initial"]
        values[name] = str(value)
    for name, value in task.get("overrides", {}).items():
        if name not in values:
            raise ValueError(f"Unknown parameter override: {name}")
        values[name] = str(value)
    dest = run / "netlist" / "input.scs"
    dest.parent.mkdir(parents=True, exist_ok=True)
    generator = cfg.get("netlist", {}).get("generator")
    if generator:
        function = load_function(generator, Path(cfg["_project_root"]))
        source = Path(function(cfg, task, run)).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source != dest:
            shutil.copyfile(source, dest)
    else:
        source = local_path(Path(cfg["_project_root"]), cfg["template"])
        text = source.read_text(encoding="utf-8")
        tokens = set(re.findall(r"@@([A-Za-z][A-Za-z0-9_]*)@@", text))
        unknown = tokens - values.keys()
        if unknown:
            raise ValueError(f"Missing parameter definitions: {sorted(unknown)}")
        for name, value in values.items():
            if "\n" in value or "\r" in value or "@" in value:
                raise ValueError(f"Unsafe value for {name}")
            text = text.replace(f"@@{name}@@", value)
        if re.search(r"@@[A-Za-z][A-Za-z0-9_]*@@", text):
            raise ValueError("Unresolved placeholders")
        dest.write_text(text, encoding="utf-8")
    result = {
        "status": "READY", "created_at": utc_now(), "source": str(source),
        "source_sha256": sha256(source), "netlist": str(dest),
        "netlist_sha256": sha256(dest), "parameters": values,
    }
    write_json(run / "netlist_result.json", result)
    transition(run, "NETLIST_READY")
    return result
