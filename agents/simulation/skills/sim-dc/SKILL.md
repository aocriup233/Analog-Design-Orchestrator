---
name: sim-dc
description: Configure or inspect a DC operating-point Spectre analysis rule in this Analog Agent project.
---

# DC rule

Edit a shareable JSON rule shaped like `agents/simulation/config/dc.json`, or place site-specific options in `private/`. The reusable `analog_agent.sim_rules.render_rule` function turns the rule into a Spectre DC statement. Use `agents/simulation/scripts/build_deck.py RUN_DIR` to inspect the generated deck before submission. Keep the design netlist unchanged.

Use the DC result to check bias, device operating points, current, and power through project-specific metric functions. Deck generation alone does not establish simulation success.
