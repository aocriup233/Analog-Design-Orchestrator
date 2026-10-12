"""Read-only input verification shared by every deterministic stage."""
from pathlib import Path
from functools import wraps

from .core import local_path, read_json, sha256

_CANVAS_MODULES = ("packages/project-protocol/dist/index.js", "packages/netlist/dist/index.js")


class InputMismatch(ValueError):
    """A pinned input changed; callers must not mutate the run on this error."""


def checked_stage(stage):
    """Gate outside stage exception handlers so mismatch leaves state intact."""
    def decorate(function):
        @wraps(function)
        def checked(run, *args, **kwargs):
            try:
                verify_inputs(stage, run)
                return function(run, *args, **kwargs)
            except BaseException as error:
                from .failures import record_failure
                # Never manufacture a run or lose the primary error if recording fails.
                if Path(run).is_dir() and (Path(run) / "task.json").is_file():
                    try:
                        record_failure(run, stage, error)
                    except (OSError, ValueError):
                        pass
                raise
        return checked
    return decorate


def _check(path, expected):
    try:
        actual = sha256(path) if path.is_file() else "MISSING"
    except OSError:
        actual = "UNREADABLE"
    if actual != expected:
        raise InputMismatch(f"Input changed (hash mismatch): {path}; expected={expected}; actual={actual}")


def initial_inputs(run, cfg):
    """Pin config, private manifest and declared project inputs at creation."""
    from .campaign import _declared_inputs
    root = Path(cfg["_project_root"])
    entries = [{"scope": "run", "path": name, "sha256": sha256(local_path(run, name))}
               for name in ("config.json", "private_manifest.json")]
    names = _declared_inputs(cfg)
    names[local_path(root, cfg["_config_path"]).relative_to(root).as_posix()] = sha256(Path(cfg["_config_path"]))
    runtime_config = cfg.get("netlist", {}).get("block", {}).get("canvas_root_config")
    if runtime_config:
        names[runtime_config] = sha256(local_path(root, runtime_config))
    names.update(read_json(local_path(run, "private_manifest.json")))
    entries += [{"scope": "project", "path": name, "sha256": digest}
                for name, digest in sorted(names.items())]
    block = cfg.get("netlist", {}).get("block", {})
    if block.get("kind") == "canvas" and not block.get("adapter"):
        runtime = read_json(local_path(root, runtime_config)) if runtime_config else {}
        canvas_root = Path(runtime.get("canvas_root", block.get("canvas_root", ""))).resolve()
        entries += [{"scope": "canvas", "path": name, "sha256": sha256(local_path(canvas_root, name))}
                    for name in _CANVAS_MODULES]
    return entries


def pin_handoffs(run, task):
    """Append newly published immutable handoffs; never rebaseline old hashes."""
    if "pinned_inputs" not in task or task.get("kind") == "layout":
        return
    entries = task["pinned_inputs"]
    existing = {(e["scope"], e["path"]) for e in entries}
    for name in ("netlist_result.json", "simulation_plan.json", "staging_result.json", "submission_intent.json",
                 "submission_result.json", "retrieval_result.json", "simulation_result.json",
                 "analysis_result.json"):
        path = local_path(run, name)
        if path.is_file() and ("run", name) not in existing:
            entries.append({"scope": "run", "path": name, "sha256": sha256(path)})


def _pairs(value, root, project=None, seen=None):
    """Verify file/hash pairs in already-pinned JSON (never execute plugins)."""
    if seen is None:
        seen = set()
    if isinstance(value, list):
        for item in value:
            _pairs(item, root, project, seen)
    elif isinstance(value, dict):
        for key, name in value.items():
            digest_key = key + "_sha256"
            if isinstance(name, str) and digest_key in value:
                _check(local_path(root, name), value[digest_key])
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            base = project if project is not None and value["path"].startswith("private/") else root
            path = local_path(base, value["path"])
            _check(path, value["sha256"])
            if path.suffix == ".json" and path not in seen and base == root:
                seen.add(path)
                _pairs(read_json(path), root, project, seen)
        for mapping in ("raw_reports", "files"):
            if isinstance(value.get(mapping), dict):
                base = local_path(root, value["directory"]) if mapping == "files" and isinstance(value.get("directory"), str) else root
                for name, expected in value[mapping].items():
                    if isinstance(expected, str):
                        _check(local_path(base, name), expected)
        if isinstance(value.get("simulation_copy"), str) and "simulation_sha256" in value:
            _check(local_path(root, value["simulation_copy"]), value["simulation_sha256"])
        for key, item in value.items():
            if key not in {"data", "metadata", "parameters", "params", "canonical"}:
                _pairs(item, root, project, seen)


def verify_inputs(stage, run):
    """Recompute pinned bytes before work. No state/log/file changes on mismatch.

    Legacy schematic runs without a creation inventory are rejected: never trust
    newly computed hashes as evidence of the original inputs.
    """
    run = Path(run).resolve()
    task = read_json(local_path(run, "task.json"))
    root = run.parent.parent
    if task.get("kind") == "layout":
        _layout_inputs(run, task, stage)
        return
    if "pinned_inputs" not in task:
        raise InputMismatch("Missing pinned input inventory; preserve evidence and create a new run")
    for entry in task["pinned_inputs"]:
        scope = entry["scope"]
        if scope == "canvas":
            # Only the two explicitly configured runtime modules; no arbitrary
            # external scope and no weakened project-local path validator.
            if entry["path"] not in _CANVAS_MODULES:
                raise InputMismatch("Unknown pinned Canvas runtime module")
            from .core import run_config
            cfg = run_config(run)
            block = cfg.get("netlist", {}).get("block", {})
            if block.get("kind") != "canvas" or block.get("adapter"):
                raise InputMismatch("Canvas runtime inventory does not match configuration")
            runtime = read_json(local_path(root, block["canvas_root_config"])) if block.get("canvas_root_config") else {}
            canvas_root = Path(runtime.get("canvas_root", block.get("canvas_root", ""))).resolve()
            _check(local_path(canvas_root, entry["path"]), entry["sha256"])
            continue
        if scope not in {"run", "project"}:
            raise InputMismatch("Invalid input inventory scope")
        if stage == "reconcile" and scope == "run" and entry["path"] == "submission_result.json" and not local_path(run, entry["path"]).exists():
            continue  # Only explicit reconcile may recover this missing receipt.
        expected = entry["sha256"]
        if "reconciliation" in entry:
            proof = entry["reconciliation"]
            proof_path = local_path(run, proof["path"])
            _check(proof_path, proof["sha256"])
            record = read_json(proof_path)
            if (scope != "run" or entry["path"] != "submission_result.json" or
                    record["original_sha256"] != expected or record["path"] != entry["path"]):
                raise InputMismatch("Invalid explicit submission reconciliation receipt")
            _check(local_path(run, "submission_intent.json"), record["intent_sha256"])
            expected = record["replacement_sha256"]
        _check(local_path(run if scope == "run" else root, entry["path"]), expected)
    # Check membership too: an added/deleted private overlay changes semantics.
    from .core import private_manifest
    expected = read_json(local_path(run, "private_manifest.json"))
    current = private_manifest(root)
    for name in sorted(expected.keys() | current.keys()):
        if expected.get(name) != current.get(name):
            raise InputMismatch(f"Input hash mismatch: {name}; expected={expected.get(name, 'ABSENT')}; actual={current.get(name, 'MISSING')}")
    for entry in task["pinned_inputs"]:
        if entry["scope"] == "run" and entry["path"] not in {"config.json", "private_manifest.json"}:
            if stage == "reconcile" and entry["path"] == "submission_result.json" and not local_path(run, entry["path"]).exists():
                continue
            payload = read_json(local_path(run, entry["path"]))
            if (stage == "linked_schematic" and task["status"] == "ANALYZED" and task.get("raw_cleaned")
                    and entry["path"] == "retrieval_result.json"):
                # Trusted after-analysis cleanup removes outputs that are not
                # inputs to layout. The retrieval receipt bytes remain pinned;
                # layout still checks netlist, simulation and analysis evidence.
                payload = {**payload, "backend_data": {k: v for k, v in payload["backend_data"].items() if k != "artifacts"}}
            _pairs(payload, root)


def _layout_inputs(run, task, stage):
    root = run.parent.parent
    path = local_path(run, "layout_inputs.json")
    _check(path, task["inputs_sha256"])
    inputs = read_json(path)
    for name, expected in inputs["private_sha256"].items():
        _check(local_path(root, name), expected)
    for pinned in inputs.get("verification_runsets", {}).values():
        _check(Path(pinned["path"]), pinned["sha256"])
    source = local_path(root, "runs/" + inputs["schematic"]["run_id"])
    # Existing linked run receipts supply the legacy evidence contract as well.
    for name, key in (("netlist_result.json", "netlist_result_sha256"),
                      ("simulation_result.json", "simulation_result_sha256"), ("config.json", "config_sha256")):
        _check(local_path(source, name), inputs["schematic"][key])
    _pairs(read_json(local_path(source, "netlist_result.json")), root)
    _check(local_path(source, "analysis_result.json"), inputs["spec_gate"]["analysis_sha256"])
    if "pinned_inputs" in read_json(local_path(source, "task.json")):
        verify_inputs("linked_schematic", source)
    for key, ref in task.items():
        if (key.startswith("active_") or key in {"constraints_draft", "floorplan_export", "inherited_floorplan_freeze"}) and isinstance(ref, dict) and {"path", "sha256"} <= ref.keys():
            receipt = local_path(run, ref["path"])
            _check(receipt, ref["sha256"])
            _pairs(read_json(receipt), run, root)
    frozen = task.get("active_constraints_freeze")
    draft = task.get("constraints_draft") if stage == "layout_constraints_freeze" else None
    if frozen or draft:
        document = read_json(local_path(run, (frozen or draft)["path"]))
        document = document["document"] if frozen else document
        provenance = document["inputs"]
        from .layout_constraints import USER_PATH
        user = local_path(root, USER_PATH)
        expected = provenance["user_sha256"]
        if expected is not None or user.exists():
            try:
                _check(user, expected if expected is not None else "ABSENT")
            except InputMismatch as exc:
                raise InputMismatch(f"{exc}; regenerate and review the draft") from exc
        memory = provenance.get("memory", {})
        if memory.get("source_sha256"):
            from .core import run_config
            project = run_config(source)
            _check(local_path(root, project["memory"]["path"]), memory["source_sha256"])
    # Rule decks are external by explicit private configuration, never a generic
    # relaxation of local_path. Prior report hashes prove consumed rule identity.
    from .layout_config import load_layout_config, verification_inputs
    cfg, manifest = load_layout_config(root)
    if manifest != inputs["private_sha256"]:
        expected = inputs["private_sha256"]
        for name in sorted(manifest.keys() | expected.keys()):
            if manifest.get(name) != expected.get(name):
                raise InputMismatch(f"Input hash mismatch: {name}; expected={expected.get(name, 'ABSENT')}; actual={manifest.get(name, 'MISSING')}")
    for phase, pinned in inputs.get("verification_runsets", {}).items():
        if phase not in {"drc", "lvs"} or pinned["path"] != cfg[phase].get("runset"):
            raise InputMismatch("Runset inventory does not match private configuration")
        _check(Path(pinned["path"]), pinned["sha256"])
    for name in ("drc_report.json", "lvs_report.json"):
        report_path = local_path(run, name)
        if report_path.is_file():
            report = read_json(report_path)
            hashes = report.get("rule_sha256", {})
            if hashes:
                for filename, expected in hashes.items():
                    section = cfg[report["stage"]]
                    candidate = Path(filename).resolve()
                    rule_root = Path(section["rule_dir"]).resolve()
                    if not candidate.is_relative_to(rule_root) and candidate != Path(section.get("runset", "")).resolve():
                        raise InputMismatch("Pinned rule input escapes its private rule/runset boundary")
                    _check(candidate, expected)
                actual = verification_inputs(root, cfg, report["stage"], list(hashes))["rule_sha256"]
                for filename, digest in hashes.items():
                    if actual[filename] != digest:
                        raise InputMismatch(f"Input hash mismatch: {filename}; expected={digest}; actual={actual[filename]}")
