---
name: sim-stb
description: Configure or inspect a netlist-driven Spectre STB loop-stability analysis rule in Analog Agent.
---

# STB rule

Copy `agents/simulation/config/stb.json` into the circuit project and set
`options.probe` to an actual loop probe instance in that project's design
netlist. `LOOP_PROBE` in the shared example is a placeholder, not a usable
device name. Set `start`, `stop`, and a sweep density (`dec`, `lin`, or `log`)
for the frequency range of interest.

`analog_agent.sim_rules.render_rule` validates and renders the analysis
statement; `agents/simulation/scripts/build_deck.py RUN_DIR` previews the
separate simulation deck. STB configuration does not by itself establish a
valid loop break, phase convention, or stability margin. Verify those from
the actual circuit and raw results. Keep circuit-specific trace bindings,
margin extraction, and acceptance thresholds in project-owned analysis
plugins and rules.
