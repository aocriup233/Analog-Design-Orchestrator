---
name: sim-tran
description: Configure or inspect a transient Spectre analysis rule in this Analog Agent project.
---

# Transient rule

Edit a JSON rule shaped like `agents/simulation/config/tran.json`. `stop` is required; add supported Spectre options such as `maxstep`, `errpreset`, or strobing as the circuit needs. `analog_agent.sim_rules.render_rule` renders it and `agents/simulation/scripts/build_deck.py RUN_DIR` previews the deck. Choose a save list that covers the measurements without dumping every node.

Use waveform analysis scripts for timing, eye, sample, reference, and FFT work. Keep time origin, units, and sampling interval explicit in the project config.
