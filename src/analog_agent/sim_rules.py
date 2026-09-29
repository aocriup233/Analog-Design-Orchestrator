"""Compose Spectre analysis decks from reusable, validated rule files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .core import local_path, read_json, sha256, write_json
from .plugins import load_function


_ATOM = re.compile(r"^[A-Za-z0-9_+./*()^<>:=,\\-]+$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _atom(value: Any, field: str) -> str:
    string = str(value)
    if not _ATOM.fullmatch(string):
        raise ValueError(f"Unsafe Spectre atom in {field}: {string!r}")
    return string


def render_rule(rule: dict[str, Any], project_root: Path | None = None) -> str:
    kind = rule.get("kind")
    if kind == "python":
        if project_root is None:
            raise ValueError("Custom analysis rule needs a project root")
        function = load_function(rule["function"], project_root)
        statement = str(function(rule)).strip()
        if not statement:
            raise ValueError("Custom analysis rule returned an empty statement")
        return statement
    if kind not in {"dc", "ac", "tran", "stb"}:
        raise ValueError(f"Unsupported analysis kind: {kind}")
    name = str(rule.get("name", {"dc": "dcOp", "ac": "ac", "tran": "tran", "stb": "stb"}[kind]))
    if not _NAME.fullmatch(name):
        raise ValueError(f"Invalid analysis name: {name}")
    options = dict(rule.get("options", {}))
    if kind in {"ac", "stb"} and not {"start", "stop"} <= options.keys():
        raise ValueError(f"{kind.upper()} requires start and stop")
    if kind in {"ac", "stb"} and not any(key in options for key in ("dec", "lin", "log")):
        raise ValueError(f"{kind.upper()} requires dec, lin, or log sweep density")
    if kind == "stb" and not options.get("probe"):
        raise ValueError("STB requires a probe instance")
    if kind == "tran" and "stop" not in options:
        raise ValueError("Transient analysis requires stop")
    rendered = []
    for key, value in options.items():
        if not _NAME.fullmatch(key):
            raise ValueError(f"Invalid option name: {key}")
        rendered.append(f"{key}={_atom(value, key)}")
    return " ".join([name, kind, *rendered])


def build_deck(design: Path, config: dict[str, Any], run: Path) -> dict[str, Any]:
    """Create a simulator-owned deck; leave the authored design untouched."""
    root = Path(config["_project_root"])
    simulation = config.get("simulation", {})
    selected = read_json(run / "task.json").get("testbench") or config.get("netlist", {}).get("default_testbench")
    paths = simulation.get("rules_by_testbench", {}).get(selected, simulation.get("rules", []))
    if not paths:
        raise ValueError("simulation.rules is empty")
    rules = []
    for relative in paths:
        path = local_path(root, relative)
        rule = read_json(path)
        rules.append({"path": str(path), "sha256": sha256(path),
                      "kind": rule.get("kind"), "name": rule.get("name")})
    statements = [render_rule(read_json(Path(item["path"])), root) for item in rules]
    if len({line.split()[0] for line in statements}) != len(statements):
        raise ValueError("Duplicate analysis names")
    signals = simulation.get("save_signals", [])
    if signals and not isinstance(signals, list):
        raise ValueError("save_signals must be an array")
    saves = [f"save {' '.join(_atom(signal, 'save signal') for signal in signals)}",
             "saveOptions options save=selected"] if signals else []
    if design.name != "input.scs":
        raise ValueError("The prepared design must be input.scs")
    deck = run / "netlist" / "simulation.scs"
    deck.write_text("\n".join([
        "simulator lang=spectre", 'include "input.scs"',
        "// Generated from user-configured analysis rule files",
        *statements, *saves, "",
    ]), encoding="utf-8")
    manifest = {"deck": str(deck), "deck_sha256": sha256(deck),
                "design_sha256": sha256(design), "rules": rules,
                "statements": statements, "save_signals": signals,
                "testbench": selected}
    write_json(run / "simulation_plan.json", manifest)
    return manifest
