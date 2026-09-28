# Analog Agent working rules

This project uses three separately executed roles. Each role communicates through files in a unique `runs/<run_id>/` directory. Read the matching role guide in `agents/` before acting.

Shared project guidance lives in `common/skills/analog-project/SKILL.md`. Role skills live under `agents/netlist/skills/`, `agents/simulation/skills/`, and `agents/analysis/skills/`. Read the relevant `SKILL.md` completely when that role or analysis mode applies. Site-specific knowledge belongs in ignored `private/`; start from `private.example/` and never expose private config values in reports.

- Netlist agent: `agents/netlist.md`. Owns template choice, parameter proposals, and `netlist_result.json`. It never launches Spectre.
- Simulation agent: `agents/simulation.md`. Owns DC/AC/tran rule files, the generated simulation deck, and `simulation_result.json`. It never edits a prepared design netlist.
- Analysis agent: `agents/analysis.md`. Owns metric extraction, reusable waveform plots/calculations, verdicts, and `analysis_result.json`. It never reruns Spectre implicitly.

Prefer the reusable functions in `src/analog_agent/` and the installed `virtuoso_bridge` package. A human or an LLM controller may choose the next candidate, but the local workers execute and verify it. Never claim a simulation succeeded from a submission message; use the recorded bridge status and artifacts. For remote operations, show actual captured tool output at consequential steps; never reconstruct output.

For a sweep or iterative project, use `analog-agent campaign` and the `agents/analysis/skills/campaign-review` skill: one durable campaign spans multiple cycles, and a cycle runs user/analysis-AI-selected points with bounded parallelism. The logical roles persist through campaign artifacts; worker OS processes may exit between ticks. After any context/token interruption, run `analog-agent campaign doctor CAMPAIGN_DIR` and `analog-agent campaign brief CAMPAIGN_DIR` before `campaign run` or `campaign step`. Do not retry an uncertain submission without reconciling its actual scheduler job. Initial and next-point selection may come from a private `campaign.proposer`; review its proposal before adding a cycle. A completed comparison needs a human/analysis-AI decision before another cycle or closure.

No role may delete another role's artifacts. Only the workflow cleanup function may remove `runs/<run_id>/raw`, after a passing analysis and when configured. The bridge's separate remote cleanup is governed by `simulation.keep_remote_files`.
