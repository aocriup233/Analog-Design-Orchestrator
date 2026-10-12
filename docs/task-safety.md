# S2: input gates and task monitoring

## Read-only hash gate

`input_gate.verify_inputs(stage, run)` runs before netlist preparation, simulation
staging/submission/poll/retrieval/verification, analysis, role launch, and layout
export/freeze/constraints/generation/routing proposal/route/verification/fix.
Layout decorators run outside stage exception handlers: an input mismatch never
advances the state or invokes a Bridge/adapter. Errors include the file and its
expected/actual SHA-256; missing files say `MISSING`. No file contents are printed.

New schematic tasks contain `pinned_inputs`: run-confined config and private
manifest byte hashes, original public project config, private overlays, declared
templates/includes/rules/local plugins and additional campaign dependencies.
Transitions append newly published immutable handoffs, without replacing existing
expected hashes. The gate checks those handoffs and their referenced netlist,
block/TB, deck, rule and retrieved-artifact bytes. Added private overlays are also
rejected. Private values are never copied into the public config snapshot.

Layout uses its existing task-pinned `layout_inputs.json`, linked schematic
receipts, private layout/plugin manifest, active freeze/review/route/report
references and nested artifact hashes. Consumed external verification rule files
remain confined to configured rule directories or the exact runset. Local paths
still use `core.local_path`; external rule checking is not a general escape hatch.
Configured runsets are pinned at layout preparation; rule includes are pinned
when their consumption is declared. Frozen user constraints and memory sources
are checked before execution. Draft source revisions remain reviewable through
regeneration, but tampered draft receipts cannot be frozen.
When linking an ANALYZED schematic whose trusted workflow marked `raw_cleaned`,
layout checks all retained handoffs but does not require the deliberately removed
raw output files: those waveforms are not layout inputs. Electrical stages do not
receive this exception and still verify retrieved artifacts before analysis.

These are integrity gates, not signed attestations or filesystem snapshots. The
task record is the trusted local hash anchor. Concurrent editing after the gate
remains a race; adapters must retain their existing post-use checks. Installed
module/transitive dependency pinning is a deployment responsibility; declared
project-local dependencies are pinned, but undeclared plugin reads cannot be
inferred. Native verification adapters must declare every consumed rule include.

Legacy schematic runs without the creation inventory fail closed at stage entry.
Do not synthesize an inventory from current bytes and call it original evidence:
preserve the old run and create a new one. Layout's existing linked receipt
contract remains supported, with the new inventory checked when present.

An explicit LSF `reconcile` can reconstruct a missing submission receipt after
the adapter locates the actual job. A uniquely named, hash-pinned
`submission_reconciliations/` proof records the original expected hash, replacement
hash, intent hash and job ID. The original expected hash remains in the inventory;
ordinary execution cannot silently refresh it. No job is submitted by reconcile.

## A single monitor contract

Optional public configuration:

```json
{"monitor": {"timeout_s": 3600, "heartbeat_timeout_s": 120,
             "progress_timeout_s": 600, "poll_interval_s": 5}}
```

All four values must be finite positive numbers. Each task gets an exclusive
`task_monitors/<id>/` directory with atomic `handoff.json` and `heartbeat.json`.
The handoff records owner PID/host, local PID or LSF job ID when known, policy,
start/progress times, observed state and every observed state change. Existing
simulator/role/reader timeouts still apply; the monitor bounds simulation staging,
submission, poll, retrieval and verification calls. The monitor itself performs
no network, AI or scheduler submission.

`analog-agent inspect RUN` and `status RUN` show a derived `STALLED` status and
alerts when heartbeat, progress or total timeout expires. The durable circuit
state remains unchanged. Campaign inspection exposes `monitor_status` separately
from the actual run state; layout status also lists monitors. Inspection never
writes, cleans or resubmits. A dead monitor cannot record its own death: inspection
derives that alert from the last persisted heartbeat rather than fabricating a
backend FAILED/DONE state.

`analog-agent monitor RUN --max-seconds 60` periodically polls already submitted
LSF jobs, or observes local task handoffs. DONE still requires normal retrieval/
verification through `resume`; monitoring never equates DONE with VERIFIED.
It never enters submit/stage, retries or reconciles. It stops on STALLED, completion
or its bounded watch duration. Existing `resume`/campaign ticks use the same
monitor records; no autonomous scheduler daemon is installed. Poll adapters may
return a JSON-safe `progress` token (for
example a solver checkpoint). Only changed tokens or backend states reset the
progress clock; a poll timestamp or monitor heartbeat does not. A long queued job
can legitimately alert: choose thresholds for the site's queue and resume cadence.
Local reader/adapter PID watchers poll periodically and observe diagnostic/result
file sizes; those are activity indicators, not evidence of electrical convergence.

Synchronous simulation and trusted adapter calls run behind a bounded daemon
thread. Python cannot safely kill arbitrary trusted adapter code: after timeout
the call may still be running, locally or remotely. Its late return cannot publish
a simulation handoff, but adapter side effects may continue. Preserve the
submission intent, inspect the actual process/job and reconcile before any retry.
The existing reader process-group termination policy remains intact; killing a
local reader does not cancel a remote CIW operation. Local simulator internals are
opaque to ADO: only the runner's enclosing call is observed unless the deployment
exposes an actual simulator PID/progress token. No orphan-cleanup claim is made.

Explicit successful reconciliation retires stalled simulation monitors as
`RECONCILED` and starts a monitor for the adopted job; old evidence remains. No
timeout or stalled status invokes reconcile automatically. Retry classification,
backoff and attempt-directory rollback belong to S3, not this stage.

## Tests

Fake backends and temporary projects exercise config/manifest/rule/plugin/netlist/
receipt/rule-deck tampering, state preservation and blocked Bridge calls; fake
clocks exercise stopped heartbeats and absent progress. Hanging/crashed adapter
tests preserve intent and assert a single submit; local process tests record exit
and failure. Existing asynchronous resume/reconcile, layout and doctor regressions
remain required. No real simulator, Bridge or LLM service is used by this stage.
