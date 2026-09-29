# Simulation agent

Input: an existing `netlist_result.json` and the immutable config snapshot in the run directory.

Use `analog-agent submit <run_dir>`. The worker checks the design netlist hash, builds or verifies `simulation.scs` from DC/AC/tran rules, and stages inputs through the configured backend. Bridge and local Spectre finish synchronously. An LSF adapter returns a job ID and the worker persists `SUBMITTED`; use `analog-agent resume <run_dir>` to poll once and, after `DONE`, retrieve and verify local results. A process restart can continue from the durable handoffs. Do not edit prepared inputs or infer success from a submission message.

Handoffs: `staging_result.json`, `submission_result.json`, `poll_result.json`, `retrieval_result.json`, then `simulation_result.json` only after verification. DC/AC/STB/tran statements come from project rule files; STB probes and phase-margin interpretation remain circuit-specific. Bridge remote retention uses `keep_remote_files`; an LSF adapter may clean verified staging files through `cleanup-remote`. Site-specific credentials and topology remain private.
