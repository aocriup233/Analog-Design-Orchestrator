"""Run the reusable netlist authoring function for an existing run."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from analog_agent.netlist_agent import prepare


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: render_netlist.py RUN_DIR")
    print(json.dumps(prepare(Path(sys.argv[1])), indent=2))
