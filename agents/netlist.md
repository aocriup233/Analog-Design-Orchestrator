# Netlist design agent

Input: `project.json`, a run's `task.json`, and optionally the previous `analysis_result.json`.

Decide the next legal design parameter values and, when needed, the circuit topology. Use `analog-agent new project.json --iteration N --set NAME=VALUE` to create an immutable candidate. The local `netlist_agent.prepare()` function renders declared `@@NAME@@` tokens or invokes the configured project-local generator, then records source and output SHA-256 hashes. Keep model includes in the Spectre template or `simulation.include_files` as appropriate. Do not guess PDK cell names or parameters; check the installed model and bridge documentation.

For a new topology, edit a source template in the project, then create a new run. Never edit `runs/<run_id>/netlist/input.scs` after it has been handed to simulation. Do not run Spectre from this role.

Handoff: `netlist_result.json` with netlist path, hash, chosen parameters, and source hash.
