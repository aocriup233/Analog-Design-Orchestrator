"""Simulator backends. Site transport lives behind the same phase contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .core import sha256
from .plugins import load_function


def json_safe(value: Any) -> Any:
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SimulationBackend(Protocol):
    def stage(self, run: Path, config: dict, plan: dict) -> dict: ...
    def submit(self, run: Path, config: dict, staged: dict) -> dict: ...
    def poll(self, run: Path, config: dict, submitted: dict) -> dict: ...
    def retrieve(self, run: Path, config: dict, submitted: dict) -> dict: ...
    def verify(self, run: Path, config: dict, submitted: dict, retrieved: dict) -> dict: ...
    def cleanup(self, run: Path, config: dict, submitted: dict) -> dict: ...


class BridgeBackend:
    """Synchronous Bridge runner; ``local=True`` selects local Spectre."""

    def __init__(self, local: bool = False, simulator_factory=None):
        self.local = local
        self.simulator_factory = simulator_factory

    def stage(self, run: Path, config: dict, plan: dict) -> dict:
        design = run / "netlist" / "input.scs"
        includes = [str(Path(config["_project_root"]) / p) for p in
                    config.get("simulation", {}).get("include_files", [])]
        if Path(plan["deck"]).resolve() != design.resolve():
            includes.insert(0, str(design))
        return {"input": plan["deck"], "input_sha256": plan["deck_sha256"],
                "include_files": includes}

    def submit(self, run: Path, config: dict, staged: dict) -> dict:
        opts = config.get("simulation", {})
        if self.simulator_factory is None:
            from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args
            shared = {
                "spectre_cmd": opts.get("spectre_cmd", "spectre"),
                "spectre_args": spectre_mode_args(opts.get("mode", "ax")),
                "timeout": int(opts.get("timeout_s", 600)),
                "work_dir": run / "raw",
                "output_format": opts.get("output_format", "psfascii"),
            }
            if self.local:
                simulator = SpectreSimulator.local(**shared)
            else:
                simulator = SpectreSimulator.from_env(
                    **shared, keep_remote_files=bool(opts.get("keep_remote_files", False)),
                    profile=opts.get("profile"))
        else:
            simulator = self.simulator_factory()
        result = simulator.run_simulation(Path(staged["input"]),
                                          {"include_files": staged["include_files"]})
        return {"state": "DONE" if result.ok else "FAILED",
                "result": {"ok": bool(result.ok), "bridge_status": result.status.value,
                           "errors": json_safe(result.errors), "warnings": json_safe(result.warnings),
                           "metadata": json_safe(result.metadata), "data": json_safe(result.data)}}

    def poll(self, run: Path, config: dict, submitted: dict) -> dict:
        return {"state": submitted["state"]}

    def retrieve(self, run: Path, config: dict, submitted: dict) -> dict:
        return {"artifacts": [], "data": submitted["result"]["data"]}

    def verify(self, run: Path, config: dict, submitted: dict, retrieved: dict) -> dict:
        result = submitted["result"]
        if not result["ok"]:
            raise ValueError("Bridge did not report a successful simulation")
        return {"bridge_status": result["bridge_status"], "errors": result["errors"],
                "warnings": result["warnings"], "metadata": result["metadata"],
                "data": retrieved["data"]}

    def cleanup(self, run: Path, config: dict, submitted: dict) -> dict:
        return {"status": "NOT_APPLICABLE", "reason": "Bridge handles its own remote retention"}


class LSFBackend:
    """Delegates site-specific SSH/Telnet/LSF operations to a private adapter.

    The adapter factory receives ``(config, run)`` and returns an object with
    the six phase methods in :class:`SimulationBackend`. Credentials are never
    part of this public interface or persisted handoff files.
    """

    def __init__(self, config: dict, run: Path):
        spec = config["simulation"]["adapter"]
        factory = load_function(spec, Path(config["_project_root"]))
        self.adapter = factory(config, run)

    def _call(self, name: str, *args) -> dict:
        result = getattr(self.adapter, name)(*args)
        if not isinstance(result, dict):
            raise TypeError(f"LSF adapter {name} must return an object")
        return result

    def stage(self, run: Path, config: dict, plan: dict) -> dict:
        result = self._call("stage", run, config, plan)
        if result.get("input_sha256") != plan["deck_sha256"]:
            raise ValueError("LSF staging did not confirm the deck SHA-256")
        return result

    def submit(self, run: Path, config: dict, staged: dict) -> dict:
        result = self._call("submit", run, config, staged)
        if not result.get("job_id") or result.get("state") not in {"SUBMITTED", "RUN", "DONE"}:
            raise ValueError("LSF submit must return job_id and submitted state")
        return result

    def reconcile(self, run: Path, config: dict, staged: dict, intent: dict) -> dict:
        """Optional site lookup by stable run ID after an uncertain submit."""
        if not callable(getattr(self.adapter, "reconcile", None)):
            raise NotImplementedError("Site adapter has no reconcile method; inspect its scheduler manually")
        result = self._call("reconcile", run, config, staged, intent)
        if not result.get("job_id") or result.get("state") not in {"SUBMITTED", "RUN", "DONE", "FAILED"}:
            raise ValueError("LSF reconcile must return a real job_id and valid state")
        return result

    def poll(self, run: Path, config: dict, submitted: dict) -> dict:
        result = self._call("poll", run, config, submitted)
        if result.get("state") not in {"SUBMITTED", "RUN", "DONE", "FAILED"}:
            raise ValueError("LSF poll returned an invalid state")
        return result

    def retrieve(self, run: Path, config: dict, submitted: dict) -> dict:
        result = self._call("retrieve", run, config, submitted)
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("LSF retrieval must list local artifacts")
        for artifact in artifacts:
            path = Path(artifact["path"]).resolve(strict=True)
            if not path.is_relative_to(run.resolve()):
                raise ValueError("Retrieved artifact escapes the run directory")
            actual = sha256(path)
            if actual != artifact.get("sha256") or (artifact.get("remote_sha256") and
                                                   actual != artifact["remote_sha256"]):
                raise ValueError(f"Retrieved artifact hash mismatch: {path}")
        return result

    def verify(self, run: Path, config: dict, submitted: dict, retrieved: dict) -> dict:
        result = self._call("verify", run, config, submitted, retrieved)
        if result.get("ok") is not True or not isinstance(result.get("data"), dict):
            raise ValueError("LSF verification must confirm ok and parsed data")
        return result

    def cleanup(self, run: Path, config: dict, submitted: dict) -> dict:
        return self._call("cleanup", run, config, submitted)


class ExternalBackend:
    """Stage a verified local deck for a site worker; never touch the site."""

    def stage(self, run: Path, config: dict, plan: dict) -> dict:
        return {"input": plan["deck"], "input_sha256": plan["deck_sha256"],
                "execution": "site_worker"}


def backend_for(config: dict, run: Path, simulator_factory=None) -> SimulationBackend:
    kind = config.get("simulation", {}).get("backend", "bridge")
    if kind == "lsf":
        return LSFBackend(config, run)
    if kind == "external":
        return ExternalBackend()
    return BridgeBackend(local=kind == "local", simulator_factory=simulator_factory)
