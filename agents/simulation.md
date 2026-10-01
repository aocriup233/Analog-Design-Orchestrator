# Simulation agent

Input: an existing `netlist_result.json` and the immutable config snapshot in the run directory.

Use `analog-agent submit <run_dir>`. The worker checks the design netlist hash, builds or verifies `simulation.scs` from DC/AC/tran rules, and stages inputs through the configured backend. Bridge and local Spectre finish synchronously. An LSF adapter returns a job ID and the worker persists `SUBMITTED`; use `analog-agent resume <run_dir>` to poll once and, after `DONE`, retrieve and verify local results. A process restart can continue from the durable handoffs. Do not edit prepared inputs or infer success from a submission message.

Handoffs: `staging_result.json`, `submission_result.json`, `poll_result.json`, `retrieval_result.json`, then `simulation_result.json` only after verification. DC/AC/STB/tran statements come from project rule files; STB probes and phase-margin interpretation remain circuit-specific. Bridge remote retention uses `keep_remote_files`; an LSF adapter may clean verified staging files through `cleanup-remote`. Site-specific credentials and topology remain private.

Do not stop work merely because a rule renderer finished or `submit` returned `STAGED`, `SUBMITTED`, or `RUN`. Check `analog-agent duty RUN_DIR`; these states still belong to the simulation role. For an external backend, the site worker must collect actual scheduler and simulator evidence and update the durable run before relinquishing the role. Interactive SSH/Telnet sessions belong to the agent that opened them; open and verify your own session instead of assuming another agent's session ID can be reused. A failure requires recorded job-specific evidence and a `FAILED` state, not a fabricated `DONE` result.
An external `STAGED` run may already have been submitted before an interruption: reconcile the scheduler and remote inputs before attempting another submission.
After verification or a job-specific `FAILED` record, hand the run to the analysis role and confirm its `analog-agent duty` output. A failed job does not authorize the simulation worker to edit the circuit block; analysis/user review routes the next immutable run to netlist or simulation.
