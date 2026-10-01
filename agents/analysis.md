# Result analysis agent

Input: a `DONE` simulation, `simulation_result.json`, and configured metric and rule definitions.

Use `analog-agent analyze <run_dir>`. The local `analysis_agent.analyze()` calculates scalar metrics or AC gain at a configured frequency and checks min/max rules. If the specification fails, return the failing metric and propose a new bounded parameter candidate to the netlist agent. Do not silently resubmit a simulation or infer missing values. A failed simulation cannot be analyzed as valid data.

For waveform exploration, use `agents/analysis/scripts/waveform_tool.py` with one of the JSON configs in `agents/analysis/config/`. The generic functions live in `src/analog_agent/waveforms.py`; keep any circuit-specific formula in a project or private metric plugin.

Handoff: `analysis_result.json` containing values, per-rule verdicts, pass/fail, run ID, and netlist hash. Local raw data is removed only after a passing analysis if `workflow.cleanup=after_success`.

For campaign work, review each cycle's `cycle-*_comparison.json` after all points reach a terminal state. Compare constraints, metrics, failed simulations, and optional Pareto candidates; do not declare a single optimum unless the user provided a decision rule. Record the user/AI rationale when adding the next cycle or finishing. A project-local `campaign.proposer` may suggest initial or follow-up points, but its output is a proposal, never an automatic submission. After a context interruption, start with `campaign doctor` and the saved comparison, not chat memory.

# Durable role review

After a run reaches `VERIFIED`, analyze its evidence; after `FAILED`, triage the recorded failure without claiming any electrical metrics. Producing an analysis file or reviewing a failed job does not end the analysis role by itself. Use `analog-agent duty RUN_DIR` to inspect the pending owner, then record an explicit `analog-agent decide-run` decision: `iterate --to netlist` for a circuit/model/TB revision, `iterate --to simulation` for a simulation-rule or execution revision, or `accept` only for an analyzed result reviewed as complete. The next owner creates a new immutable run; never edit a frozen run.
