"""Durable run states shared by all workers and asynchronous backends."""

from __future__ import annotations

from pathlib import Path

from .core import read_json, utc_now, write_json


TRANSITIONS = {
    "CREATED": {"NETLIST_READY", "FAILED"},
    "NETLIST_READY": {"STAGED", "FAILED"},
    "STAGED": {"SUBMITTED", "DONE", "FAILED"},
    "SUBMITTED": {"RUN", "DONE", "FAILED"},
    "RUN": {"DONE", "FAILED"},
    "DONE": {"RETRIEVED", "FAILED"},
    "RETRIEVED": {"VERIFIED", "FAILED"},
    "VERIFIED": {"ANALYZED", "FAILED"},
    "ANALYZED": set(),
    "FAILED": set(),
}


def status(run: Path) -> str:
    return read_json(run / "task.json")["status"]


def transition(run: Path, new_status: str, **fields) -> dict:
    task_path = run / "task.json"
    task = read_json(task_path)
    old = task["status"]
    if new_status not in TRANSITIONS.get(old, set()):
        raise ValueError(f"Invalid run transition: {old} -> {new_status}")
    now = utc_now()
    task.setdefault("history", [{"status": old, "at": task.get("created_at", now)}])
    task["history"].append({"status": new_status, "at": now})
    task.update(status=new_status, updated_at=now, **fields)
    write_json(task_path, task)
    return task
