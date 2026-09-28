---
name: netlist-author
description: Create or iterate a circuit-independent Spectre design netlist for this Analog Agent project, handing a hashed design to the simulation role.
---

# Netlist author

Read `agents/netlist.md`. Select a topology and legal design values using the current project's requirements and verified PDK notes. Create a new run for each candidate. Use the template renderer or `netlist.generator` local function; both are implemented in `src/analog_agent/netlist_agent.py` and callable through `agents/netlist/scripts/render_netlist.py`.

The output is a design file, not an analysis plan. The simulation role owns DC/AC/tran directives. Treat `netlist_result.json` and its SHA-256 as the handoff. Do not edit the design after handoff; create another run for a revision.
