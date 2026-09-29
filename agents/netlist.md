# Netlist design agent

Input: `project.json`, a run's `task.json`, and optionally the previous `analysis_result.json`.

Decide the next legal design parameter values and, when needed, the circuit topology. In split mode, author or export a reusable `subckt` block with explicit ordered ports, then choose a measurement-specific testbench with `analog-agent new project.json --testbench NAME --set PARAM=VALUE`. The block may be LLM-authored Spectre source or a user-drawn Analog Canvas project. Canvas's built-in exporter uses its explicit model bindings; a project-private adapter handles any additional PDK mapping. Never infer pins or connectivity from drawing geometry. The worker validates the block interface, writes separate `block.scs` and `testbench.scs`, then composes a transport-compatible `input.scs` and records all hashes.

Legacy projects may still use `template` or `netlist.generator`. Keep model includes in the block or `simulation.include_files` as appropriate. Do not guess PDK cell names or parameters; check verified project-private model knowledge. The testbench owns sources and loads, while the simulation role owns all analysis statements.

For a new topology, edit a source template in the project, then create a new run. Never edit `runs/<run_id>/netlist/input.scs` after it has been handed to simulation. Do not run Spectre from this role.

Handoff: `netlist_result.json` with composed netlist path/hash, chosen parameters and TB, and independent block/TB source/output hashes.
