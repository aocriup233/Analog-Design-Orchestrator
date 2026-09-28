"""Run a reusable scope/calculator style waveform request from JSON."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from analog_agent.waveforms import run_waveform_config


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: waveform_tool.py CONFIG.json")
    print(json.dumps(run_waveform_config(Path(sys.argv[1])), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
