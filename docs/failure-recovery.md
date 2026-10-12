# S3: failure classes, attempts and recovery

This is deterministic worker code, not an AI error classifier. Every recorded
failure has exactly one `failure.class`: `retryable`, `needs_human` or
`unknown_state`. Safe codes and next steps are recorded in attempt handoffs;
arbitrary exception strings are not copied there (they may contain secrets or
foundry text). The primary exception remains available to the caller. Preflight
errors preserve task state and do not refresh any pinned hash. If the filesystem
cannot store diagnostics, the original error is still raised; a missing receipt
is never evidence of success.

## Retry boundaries

Public configuration: `retry.max_attempts` (default 3, integer 1–10) and
`retry.base_s` (default 1, finite positive, at most 60). The limit includes the
initial call. Delay is `min(60, base_s * 2**retry_index)`, without random jitter.
Caps apply per explicit operation invocation; repeated human invocations do not
turn an uncertain attempt into a retryable one.

`RetryableError(code, no_side_effects=True)` is a trusted adapter attestation:
supported codes are `network`, `license_unavailable`, `scheduler_busy`. It means
the call returned/raised completely and no job/mutation requiring reconciliation
was performed. A submission network exception without this attestation is
**unknown**, not safe to retry. Completed `ConnectionError` and selected network
errno failures from replay-safe polling, isolated retrieval or verification can
be retried. Generic timeout is always unknown: the adapter thread may still run.
No text matching on “license” or “network” authorizes submission.

Configuration, input hashes, missing declared files, schema violations and
non-clean DRC/LVS are `needs_human`. Interrupted submission, absent job ID,
unknown adapter crashes, process interruption and interrupted writes are
`unknown_state`: no automatic retry. Typed layout adapters can return only the
audited retry code/boolean envelope; arbitrary SKILL remains forbidden. Only an
explicit no-side-effects attestation permits retry of a typed operation. A
partially executed routing plan is never rerun as a whole automatically.

## Evidence and adapter contract

Every backend operation gets an exclusive
`attempts/<operation>/<uuid>/` directory, immutable started/success/failure
events, and an append-only fsynced `attempts.jsonl` index containing their SHA-256.
Existing handoff bytes (including the mutable poll compatibility view) are copied
to exclusive `prior/` files and hashed before the operation. Successive attempts
never reuse an output directory. The current task and poll view remain mutable
coordination pointers, not immutable history; their previous evidence survives.
Snapshots/events use existing `core.local_path` and atomic `write_json`. Index
appends are serialized; stale owner locks are never reclaimed automatically.

Trusted simulation adapters receive `_attempt_dir`, `_run_dir` and `_run_id` in
their runtime config (not the public config snapshot). The factory and
stage/submit/poll/verify retain the original run identity. Bridge/local raw output
uses `_attempt_dir/raw`. **LSF retrieve now receives the attempt output directory
as its `run` argument**; relative artifact paths resolve there. Use `_run_id` or
the submitted job receipt for remote identity, not that output directory's name.
This is an intentional site-adapter compatibility change. Write only new outputs
there; never overwrite original run evidence. LSF hashes still require the remote
digest unless explicitly opted out. Layout retains its existing exclusive
execution/result and verification-output directories; attempts index their calls.
Adapters are trusted Python, not sandboxed: the contract cannot prevent a
malicious adapter from writing elsewhere or lying about side effects.

`record_rollback` writes a new evidence-only rollback proposal with source hash
and review reason. It never copies old results over new ones, changes state or
re-executes a design. Corrected designs still require new immutable runs; existing
layout freeze/route/fix review gates are unchanged.

## Token/context/process interruptions

ADO cannot observe the desktop's token/credit exhaustion event directly. Durable
files are the recovery source, not chat memory. On return run
`analog-agent recover RUN`: read-only hash verification, attempt/heartbeat checks,
lock owner reporting and incomplete submission detection. It returns non-zero
for STALLED/unknown execution, never submits, clears locks or cancels jobs.
`status`/`inspect` also show attempts and `reconcile_required`. A RUNNING attempt
without its terminal indexed event is inferred as unknown, even if the process
died before recording an exception. Missing-index started events are reported.

An existing intent without a submission receipt blocks submission. Use the
existing explicit LSF `reconcile` lookup to establish the actual scheduler job;
its hash-bound job receipt publishes a new reconciliation event, retaining all
old attempts. No lookup means no retry. Other unknown operations (including
layout/reader mutation) remain blocked for operator review; no fabricated generic
“clear unknown” command is provided. Preserve evidence and use a newly reviewed
run/clone when existing recovery cannot establish the actual state. Campaign
recovery still starts with `campaign doctor` and `campaign brief`; points now
record failure classes alongside the original error.

Successful safe retries leave terminal RETRYABLE_FAILED monitors as evidence,
not perpetual STALLED alerts. Failure after the hard cap is still recorded and
raised. Timeout does not kill arbitrary trusted threads or remote processes;
reconciliation and the existing remote cleanup inventory duties still apply.

## Fault injection

Fake-backend tests cover license unavailable then available, scheduler backoff
cap, completed network query failure, uncertain network timeout, missing job ID,
interrupt between intent and receipt, atomic JSON replacement failure, partial
retrieval, stale locks, abrupt worker death, tampering, rollback evidence and
read-only recovery. They assert classes, circuit states, unchanged prior bytes
and no duplicate submission. No simulator, Bridge, scheduler or AI is contacted.
STOP after the S3 PR. Real smallest-cell layout execution is a separate request.
