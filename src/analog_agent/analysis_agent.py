"""Analysis worker: reduce simulator data to metrics and rule verdicts."""

from __future__ import annotations

import math
from pathlib import Path

from .core import read_json, run_config, utc_now, write_json
from .plugins import load_function
from .state import status, transition


def _magnitude(value):
    if isinstance(value, dict) and {"real", "imag"} <= value.keys():
        return abs(complex(value["real"], value["imag"]))
    return abs(float(value))


def extract(data: dict, definition: dict, run: Path | None = None, project_root: Path | None = None) -> float:
    kind = definition["kind"]
    if kind == "scalar":
        value = data[definition["key"]]
        if isinstance(value, list):
            raise ValueError("Scalar metric points to a waveform")
        return float(value)
    if kind == "ac_db_at":
        freq = data[definition.get("frequency_key", "ac_freq")]
        signal = data[definition["signal"]]
        if not freq or len(freq) != len(signal):
            raise ValueError("AC frequency and signal lengths differ")
        target = float(definition["at_hz"])
        index = min(range(len(freq)), key=lambda i: abs(float(freq[i]) - target))
        return 20 * math.log10(max(_magnitude(signal[index]), 1e-30))
    if kind == "python":
        if run is None or project_root is None:
            raise ValueError("Python metric needs run and project root")
        function = load_function(definition["function"], project_root)
        return float(function(data, definition, run))
    raise ValueError(f"Unsupported metric kind: {kind}")


def analyze(run: Path) -> dict:
    run = run.resolve()
    cfg = run_config(run)
    sim = read_json(run / "simulation_result.json")
    if sim["status"] != "DONE":
        raise ValueError("Simulation has not completed successfully")
    if status(run) != "VERIFIED":
        raise ValueError("Simulation results have not reached VERIFIED state")
    metrics = {name: extract(sim["data"], definition, run, Path(cfg["_project_root"]))
               for name, definition in cfg.get("metrics", {}).items()}
    verdicts = {}
    for name, rule in cfg.get("rules", {}).items():
        if name not in metrics:
            raise ValueError(f"Rule refers to missing metric: {name}")
        value = metrics[name]
        verdicts[name] = (("min" not in rule or value >= float(rule["min"]))
                          and ("max" not in rule or value <= float(rule["max"])))
    payload = {"status": "PASS" if all(verdicts.values()) else "FAIL",
               "analyzed_at": utc_now(), "metrics": metrics, "verdicts": verdicts,
               "run_id": run.name, "netlist_sha256": sim["netlist_sha256"]}
    write_json(run / "analysis_result.json", payload)
    transition(run, "ANALYZED", verdict=payload["status"])
    return payload
