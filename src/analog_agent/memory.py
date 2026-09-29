"""Small, deterministic, project-private evidence memory.

The live decision layer sees only bounded summaries. Full claims and evidence
remain in the user project, and no worker treats memory text as instructions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .core import local_path, read_json, sha256, utc_now, write_json


_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DEFAULT_HARD_KEYS = ["pdk", "model_revision", "topology"]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _terms(value: Any) -> set[str]:
    if isinstance(value, str) and value.strip():
        return {value.strip().casefold()}
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return {item.strip().casefold() for item in value}
    raise ValueError("Memory scope values must be nonempty text or lists of nonempty text")


def _scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
        raise ValueError("Memory scope must be an object with named fields")
    for item in value.values():
        _terms(item)
    return value


def validate_config(cfg: dict) -> None:
    options = cfg.get("memory")
    if options is None:
        return
    if not isinstance(options, dict) or not isinstance(options.get("path"), str):
        raise ValueError("memory.path must name a project-local JSON file")
    path = local_path(Path(cfg["_project_root"]), options["path"])
    if not path.is_relative_to(Path(cfg["_project_root"]).resolve() / "private"):
        raise ValueError("memory.path must stay under the user project's private directory")
    if not path.is_file():
        raise FileNotFoundError(path)
    _scope(options.get("context", {}))
    hard = options.get("hard_keys", _DEFAULT_HARD_KEYS)
    if not isinstance(hard, list) or any(not isinstance(key, str) or not key for key in hard):
        raise ValueError("memory.hard_keys must be a list of scope keys")
    for key, default, ceiling in (("max_items", 4, 20), ("max_chars", 1200, 8000)):
        value = options.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError(f"memory.{key} must be in [1, {ceiling}]")
    _read_store(path)


def _read_store(path: Path) -> dict:
    store = read_json(path)
    entries = store.get("entries")
    if store.get("schema") != 1 or not isinstance(entries, list):
        raise ValueError("Memory file needs schema 1 and an entries array")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not _ID.fullmatch(entry["id"]):
            raise ValueError("Memory entry needs a valid id")
        if entry["id"] in seen:
            raise ValueError(f"Duplicate memory id: {entry['id']}")
        seen.add(entry["id"])
        if entry.get("status") not in {"candidate", "reviewed", "rejected"}:
            raise ValueError(f"Invalid memory status: {entry['id']}")
        if not isinstance(entry.get("kind"), str) or not 1 <= len(entry["kind"]) <= 32:
            raise ValueError(f"Memory entry needs a kind: {entry['id']}")
        summary = entry.get("summary")
        if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 300:
            raise ValueError(f"Memory summary must contain 1–300 characters: {entry['id']}")
        _scope(entry.get("scope", {}))
        if not isinstance(entry.get("version"), int) or entry["version"] < 1:
            raise ValueError(f"Memory entry needs a positive version: {entry['id']}")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence or any(
                not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str) for item in evidence):
            raise ValueError(f"Memory entry needs hashed evidence: {entry['id']}")
    return store


def _path(cfg: dict) -> Path:
    options = cfg.get("memory")
    if not isinstance(options, dict) or "path" not in options:
        raise ValueError("Configure memory.path in the user project")
    return local_path(Path(cfg["_project_root"]), options["path"])


def retrieve(cfg: dict) -> dict:
    """Hard-filter scope, then rank matches; cap serialized summaries before LLM use."""
    if not cfg.get("memory"):
        return {"schema": 1, "status": "DISABLED", "entries": []}
    options = cfg["memory"]
    path = _path(cfg)
    store = _read_store(path)
    context = _scope(options.get("context", {}))
    hard = set(options.get("hard_keys", _DEFAULT_HARD_KEYS))
    ranked = []
    for entry in store["entries"]:
        if entry["status"] != "reviewed":
            continue
        scope = entry["scope"]
        if any(key in scope and (key not in context or not _terms(scope[key]) & _terms(context[key]))
               for key in hard):
            continue
        score = sum(1 for key, value in scope.items()
                    if key in context and _terms(value) & _terms(context[key]))
        ranked.append((-score, entry["id"], entry))
    ranked.sort(key=lambda item: (item[0], item[1]))
    chosen = []
    limit = options.get("max_chars", 1200)
    for _, _, entry in ranked:
        if len(chosen) >= options.get("max_items", 4):
            break
        compact = {key: entry[key] for key in ("id", "kind", "version", "summary")}
        if len(json.dumps([*chosen, compact], ensure_ascii=False, separators=(",", ":"))) > limit:
            continue
        chosen.append(compact)
    return {"schema": 1, "status": "READY", "source_sha256": sha256(path),
            "context_sha256": _digest(context), "entries": chosen,
            "matched_count": len(ranked), "omitted_count": len(ranked) - len(chosen),
            "selected_chars": len(json.dumps(chosen, ensure_ascii=False, separators=(",", ":"))),
            "selection_sha256": _digest(chosen)}


def brief_view(snapshot: dict) -> dict:
    """Drop provenance hashes that need not consume the controller's tokens."""
    if snapshot.get("status") == "DISABLED":
        return {"status": "DISABLED", "entries": []}
    return {"status": snapshot["status"], "entries": snapshot["entries"],
            "matched_count": snapshot["matched_count"],
            "omitted_count": snapshot["omitted_count"],
            "selection_id": snapshot["selection_sha256"][:12]}


def show(cfg: dict, entry_id: str) -> dict:
    for entry in _read_store(_path(cfg))["entries"]:
        if entry["id"] == entry_id:
            return entry
    raise ValueError(f"Unknown memory id: {entry_id}")


@contextmanager
def _locked(path: Path):
    marker = path.with_name(path.name + ".lock")
    with marker.open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) != b"1":
            handle.seek(0)
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Memory is being changed by another process") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def submit_candidate(cfg: dict, candidate_path: Path) -> dict:
    """Record a proposed lesson as private data; it is not retrievable yet."""
    root = Path(cfg["_project_root"])
    candidate_path = candidate_path.resolve(strict=True)
    if not candidate_path.is_relative_to(root.resolve() / "private"):
        raise ValueError("Candidate must be inside the user project's private directory")
    proposed = read_json(candidate_path)
    entry_id = proposed.get("id")
    if not isinstance(entry_id, str) or not _ID.fullmatch(entry_id):
        raise ValueError("Candidate id must be a short identifier")
    summary = proposed.get("summary")
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 300:
        raise ValueError("Candidate summary must contain 1–300 characters")
    scope = _scope(proposed.get("scope", {}))
    kind = proposed.get("kind")
    if not isinstance(kind, str) or not 1 <= len(kind) <= 32:
        raise ValueError("Candidate kind is required")
    sources = proposed.get("evidence")
    if not isinstance(sources, list) or not sources or any(not isinstance(item, str) for item in sources):
        raise ValueError("Candidate needs project-local evidence paths")
    evidence = []
    for source in sources:
        file = local_path(root, source)
        if not file.is_file():
            raise FileNotFoundError(file)
        evidence.append({"path": source, "sha256": sha256(file)})
    path = _path(cfg)
    with _locked(path):
        store = _read_store(path)
        if any(item["id"] == entry_id for item in store["entries"]):
            raise ValueError(f"Duplicate memory id: {entry_id}")
        store["entries"].append({"id": entry_id, "kind": kind, "status": "candidate",
                                 "version": 1, "summary": summary.strip(), "scope": scope,
                                 "evidence": evidence, "created_at": utc_now()})
        write_json(path, store)
    return {"id": entry_id, "status": "candidate", "memory_sha256": sha256(path)}


def review(cfg: dict, entry_id: str, *, approve: bool, rationale: str) -> dict:
    if not rationale.strip():
        raise ValueError("Memory review requires a rationale")
    root = Path(cfg["_project_root"])
    path = _path(cfg)
    with _locked(path):
        store = _read_store(path)
        entry = next((item for item in store["entries"] if item["id"] == entry_id), None)
        if entry is None or entry["status"] != "candidate":
            raise ValueError("Only a candidate memory entry can be reviewed")
        for source in entry["evidence"]:
            file = local_path(root, source["path"])
            if not file.is_file() or sha256(file) != source["sha256"]:
                raise ValueError(f"Memory evidence changed: {source['path']}")
        entry.update(status="reviewed" if approve else "rejected", version=entry["version"] + 1,
                     reviewed_at=utc_now(), review_rationale=rationale.strip())
        write_json(path, store)
    return {"id": entry_id, "status": entry["status"], "version": entry["version"],
            "memory_sha256": sha256(path)}
