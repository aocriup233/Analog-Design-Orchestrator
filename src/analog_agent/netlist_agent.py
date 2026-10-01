"""Netlist worker: prepare independent circuit block and testbench artifacts."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .core import local_path, read_json, run_config, sha256, utc_now, write_json
from .plugins import load_function
from .state import transition


def _render(source: Path, values: dict[str, str]) -> str:
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
    return text


def _check_interface(text: str, name: str, pins: list[str]) -> None:
    matches = re.findall(r"(?im)^\s*subckt\s+([^\s(]+)\s*(?:\(([^)]*)\)|([^\n]*))", text)
    for master, parenthesized, bare in matches:
        if master.lower() == name.lower():
            actual = (parenthesized or bare).split()
            if [pin.lower() for pin in actual] == [pin.lower() for pin in pins]:
                return
            raise ValueError(f"Block {name} port order differs from declared pins: {actual}")
    raise ValueError(f"Block subckt {name} not found; explicit interface required")


def _split_prepare(cfg: dict, task: dict, run: Path, values: dict[str, str]) -> dict:
    root = Path(cfg["_project_root"])
    netlist = cfg["netlist"]
    block = netlist["block"]
    selected = task.get("testbench") or netlist["default_testbench"]
    if selected not in netlist["testbenches"]:
        raise ValueError(f"Unknown testbench: {selected}")
    bench = netlist["testbenches"][selected]
    directory = run / "netlist"
    directory.mkdir(parents=True, exist_ok=True)
    block_dest, bench_dest = directory / "block.scs", directory / "testbench.scs"
    source_key = "path" if block["kind"] == "source" else "project"
    block_source = local_path(root, block[source_key])
    dependencies = {item: sha256(local_path(root, item))
                    for item in block.get("dependencies", [])}
    adapter = block.get("adapter")
    if adapter and adapter.rsplit(":", 1)[0].endswith(".py"):
        location = adapter.rsplit(":", 1)[0]
        dependencies[location] = sha256(local_path(root, location))
    if block["kind"] == "source":
        block_text = _render(block_source, values)
    elif block.get("adapter"):
        function = load_function(block["adapter"], root)
        generated = Path(function(cfg, task, run, values)).resolve()
        if not generated.is_file():
            raise FileNotFoundError(generated)
        block_text = generated.read_text(encoding="utf-8")
    else:
        exporter = Path(__file__).with_name("canvas_export.mjs")
        process = subprocess.run(
            [block.get("node", "node"), str(exporter), str(block["canvas_root"]),
             str(block_source), str(block_dest)], capture_output=True, text=True,
            timeout=int(block.get("export_timeout_s", 60)), check=False)
        if process.returncode:
            raise ValueError(f"Analog Canvas export failed: {process.stderr.strip()}")
        block_text = block_dest.read_text(encoding="utf-8")
    bundle = block.get("model_bundle")
    if bundle:
        config_path = local_path(root, bundle["config"])
        renderer_path = bundle["renderer"].rsplit(":", 1)[0]
        dependencies[bundle["config"]] = sha256(config_path)
        dependencies[renderer_path] = sha256(local_path(root, renderer_path))
        begin, end = bundle["begin"], bundle["end"]
        if block_text.count(begin) != 1 or block_text.count(end) != 1:
            raise ValueError("Block model bundle markers must occur exactly once")
        start = block_text.index(begin)
        finish = block_text.index(end, start) + len(end)
        rendered = load_function(bundle["renderer"], root)(read_json(config_path))
        if (not isinstance(rendered, str) or not rendered.startswith(begin + "\n")
                or not rendered.endswith("\n" + end)
                or rendered.count(begin) != 1 or rendered.count(end) != 1):
            raise ValueError("Model bundle renderer returned an invalid marked region")
        block_text = block_text[:start] + rendered + block_text[finish:]
    _check_interface(block_text, block["name"], block["pins"])
    if re.search(r"(?im)^\s*\w+\s+(?:dc|ac|tran|stb)\b", block_text):
        raise ValueError("Block must not contain simulation analyses")
    block_dest.write_text(block_text.rstrip() + "\n", encoding="utf-8")
    if "path" in bench:
        bench_source = local_path(root, bench["path"])
        bench_text = _render(bench_source, values)
    else:
        function = load_function(bench["generator"], root)
        bench_source = Path(function(cfg, task, run, values, block["name"], block["pins"])).resolve()
        if not bench_source.is_file():
            raise FileNotFoundError(bench_source)
        bench_text = bench_source.read_text(encoding="utf-8")
    if re.search(r"(?im)^\s*\w+\s+(?:dc|ac|tran|stb)\b", bench_text):
        raise ValueError("Testbench must not contain analyses; configure simulation.rules")
    bench_dest.write_text(bench_text.rstrip() + "\n", encoding="utf-8")
    dest = directory / "input.scs"
    dest.write_text("simulator lang=spectre\n// Circuit block\n" + block_dest.read_text(encoding="utf-8")
                    + "\n// Testbench: " + selected + "\n" + bench_dest.read_text(encoding="utf-8"),
                    encoding="utf-8")
    return {"status": "READY", "created_at": utc_now(), "source": str(block_source),
            "source_sha256": sha256(block_source), "netlist": str(dest),
            "netlist_sha256": sha256(dest), "parameters": values,
            "block": {"path": str(block_dest), "sha256": sha256(block_dest),
                      "name": block["name"], "pins": block["pins"],
                      "kind": block["kind"], "dependencies": dependencies},
            "testbench": {"name": selected, "path": str(bench_dest),
                          "sha256": sha256(bench_dest), "source": str(bench_source),
                          "source_sha256": sha256(bench_source)}}


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
    if "block" in cfg.get("netlist", {}):
        result = _split_prepare(cfg, task, run, values)
        write_json(run / "netlist_result.json", result)
        transition(run, "NETLIST_READY")
        return result
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
        dest.write_text(_render(source, values), encoding="utf-8")
    result = {
        "status": "READY", "created_at": utc_now(), "source": str(source),
        "source_sha256": sha256(source), "netlist": str(dest),
        "netlist_sha256": sha256(dest), "parameters": values,
    }
    write_json(run / "netlist_result.json", result)
    transition(run, "NETLIST_READY")
    return result
