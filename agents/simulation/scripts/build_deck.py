"""Render configured DC/AC/tran rules without launching Spectre.

Usage: python agents/simulation/scripts/build_deck.py RUN_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from analog_agent.core import read_json, run_config
from analog_agent.sim_rules import build_deck


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: build_deck.py RUN_DIR")
    run = Path(sys.argv[1]).resolve()
    design = Path(read_json(run / "netlist_result.json")["netlist"])
    print(json.dumps(build_deck(design, run_config(run), run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
