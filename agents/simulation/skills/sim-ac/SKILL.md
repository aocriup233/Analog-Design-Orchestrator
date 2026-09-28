---
name: sim-ac
description: Configure or inspect an AC small-signal Spectre analysis rule in this Analog Agent project.
---

# AC rule

Edit a JSON rule shaped like `agents/simulation/config/ac.json`. Specify start, stop, and one sweep density (`dec`, `lin`, or `log`) in `options`. `analog_agent.sim_rules.render_rule` validates and renders the statement; `agents/simulation/scripts/build_deck.py RUN_DIR` previews the deck. Choose saved signals deliberately to control PSF size.

Use project metric plugins for gain, phase, impedance, bandwidth, stability, or noise-related calculations. Keep circuit-specific formulas outside the shared rule renderer.
