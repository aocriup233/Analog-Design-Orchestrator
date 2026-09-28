---
name: site-netlist
description: Apply a private PDK's verified device names, model includes, and legal sizing rules while authoring a design netlist.
---

# Site netlist

Copy this skill to `private/agents/netlist/skills/site-netlist/` and add verified PDK notes there. Use a project-local or private `netlist.generator` function for circuit-specific topology. Keep design parameters, model section, pin order, and generated netlist hash explicit. The simulation agent adds analyses later.
