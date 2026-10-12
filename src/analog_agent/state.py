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
    "LAYOUT_PREPARED": {"CONSTRAINTS_DRAFTED", "NEEDS_ATTENTION"},
    "CONSTRAINTS_DRAFTED": {"CONSTRAINTS_FROZEN", "NEEDS_ATTENTION"},
    "CONSTRAINTS_FROZEN": {"FLOORPLAN_PENDING", "NEEDS_ATTENTION"},
    "FLOORPLAN_PENDING": {"FLOORPLAN_FROZEN", "NEEDS_ATTENTION"},
    "FLOORPLAN_FROZEN": {"ROUTED", "FLOORPLAN_PENDING", "NEEDS_ATTENTION"},
    "ROUTED": {"DRC_DONE", "FLOORPLAN_PENDING", "NEEDS_ATTENTION"},
    "DRC_DONE": {"LVS_DONE", "FLOORPLAN_FROZEN", "FLOORPLAN_PENDING", "NEEDS_ATTENTION"},
    "LVS_DONE": {"LAYOUT_VERIFIED", "FLOORPLAN_FROZEN", "FLOORPLAN_PENDING", "NEEDS_ATTENTION"},
    "LAYOUT_VERIFIED": {"FLOORPLAN_PENDING", "NEEDS_ATTENTION"},
    "NEEDS_ATTENTION": set(),
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
    from .input_gate import pin_handoffs
    pin_handoffs(run, task)
    write_json(task_path, task)
    return task
