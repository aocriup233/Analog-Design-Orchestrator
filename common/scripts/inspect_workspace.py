"""Print a redacted project inventory without loading private values."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from analog_agent.core import PRIVATE_OVERRIDES, private_manifest, read_json


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: inspect_workspace.py PROJECT.json")
    path = Path(sys.argv[1]).resolve()
    public = read_json(path)
    netlist = public.get("netlist", {})
    result = {
        "project": str(path),
        "netlist_source": netlist.get("generator") or public.get("template")
        or netlist.get("block", {}).get("path") or netlist.get("block", {}).get("project"),
        "testbench_names": sorted(netlist.get("testbenches", {})),
        "analysis_rules": public.get("simulation", {}).get("rules", []),
        "metric_names": sorted(public.get("metrics", {})),
        "private_overrides": private_manifest(path.parent),
        "private_override_slots": list(PRIVATE_OVERRIDES),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
