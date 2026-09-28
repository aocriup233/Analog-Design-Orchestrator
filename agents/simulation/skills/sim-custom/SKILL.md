---
name: sim-custom
description: Add a verified Spectre analysis beyond DC, AC, and transient by using a project-local Python rule renderer.
---

# Custom simulation rule

Use a JSON rule with `kind: python` and `function: path/to/renderer.py:function`. The function receives the rule dictionary and returns one or more Spectre analysis statements. `analog_agent.sim_rules.build_deck` adds the statement to the simulation deck and records the rule-file hash. Verify syntax against the installed Spectre documentation and preview the deck before running.

Keep circuit topology in the design netlist. Use this extension for analyses such as noise, PSS, pnoise, sweeps, or Monte Carlo only after their required options and result shape have been checked for this installation. Put site-specific renderers under ignored `private/`.
