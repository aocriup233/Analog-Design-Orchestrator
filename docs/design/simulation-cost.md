# Simulation-cost reduction design

Status: **proposal only; not implemented**. Phase 3 changes documentation and
documentation tests only. No simulator, scheduler, campaign, or proposer behavior
changes. All field names and parent states below are proposed, not accepted CLI
configuration. PDK mappings, circuit thresholds, corner definitions and site
execution remain project-private. Proposals are never auto-submitted.

## Existing contract and shared extension

The current child-run chain remains:

```text
CREATED -> NETLIST_READY -> STAGED -> SUBMITTED -> RUN -> DONE
        -> RETRIEVED -> VERIFIED -> ANALYZED -> reviewed decision
```

Submission is not verification, and a verified result is not electrical approval.
Each child keeps the normal immutable netlist, simulation deck, receipts, JSON
handoffs and SHA-256 hashes. Simulation owns remote execution and its complete
artifact inventory/cleanup; analysis evaluates only a verified handoff (or
triages recorded failure). Preserve durable local evidence before remote cleanup.
Uncertain submission requires reconcile, never an automatic retry.

Propose a versioned evaluation parent, separate from run status, with
`evaluation_id`, parameter fingerprint, frozen input manifests, requested stage /
corner / fidelity matrix, child run IDs, and a coverage ledger. Each entry has
`stage_id`, `corner_id`, `fidelity_id`, `run_id`, evidence hashes, metrics with
units, outcome and review decision. Outcomes distinguish physical rule failure,
execution failure, missing observation and deliberate screening omission.
`NOT_EVALUATED` is not PASS, zero, or a fabricated simulator failure.

Parent transitions are proposed as `PLANNED -> EVALUATING -> REVIEW -> CONFIRMED`
or `REJECTED`; `AWAITING_RECONCILE` blocks advancement. `SCREENED_OUT` records
an explicit review decision without claiming all specifications were evaluated.
CONFIRMED requires all mandatory high-fidelity coverage and a final reviewed
decision. The parent never rewrites a child or bypasses child verification.
Existing projects remain single-stage, single-fidelity unless explicitly opted in.

Plan and policy JSON are frozen and hashed per cycle; later decisions append
their evidence references. Example parent-coverage entry (illustrative only):

```json
{
  "schema_version": 1,
  "evaluation_id": "evaluation-example",
  "stage_id": "dc-gate",
  "corner_id": "typical",
  "fidelity_id": "high",
  "run_id": null,
  "outcome": "NOT_EVALUATED",
  "evidence_sha256": {},
  "review_decision": null
}
```

## A. Staged evaluation: DC gate -> AC/STB -> tran/noise

### Data model and state machine

An evaluation plan declares ordered stages, required metrics/rules, testbench and
analysis-rule hashes, and explicit dependencies. AC/STB here means the analyses
appropriate to the project; neither stability probes nor acceptance thresholds
are hard-coded publicly. The final stage declares which tran/noise observations
are mandatory. Missing mandatory analyses prevent final acceptance.

Each stage creates a new immutable child run for the same parameter identity;
netlist/template and testbench variants are tracked rather than patched in place.
The DC child must reach VERIFIED, then ANALYZED and a reviewed gate decision
before the AC/STB child is eligible. The same handoff applies before tran/noise.
A gate pass means permission to evaluate further, not full-design PASS.
Dependency decisions identify reviewer, rule version and predecessor evidence.
An explicitly reviewed evaluation plan may preauthorize deterministic stage
advancement; absent that authority the controller stops at each review. This
does not authorize a proposer to submit its suggested points.

Gate rejection marks downstream coverage NOT_EVALUATED and parent SCREENED_OUT.
Infrastructure failure blocks the gate and is triaged, not recorded as physical
DC infeasibility. Persist gate completion before starting its successor; lock
parent advancement and use each child's submission intent for idempotency.
Operating-point reuse across analyses is a separate simulator-specific capability:
do not assume an incompatible saved bias state can be imported safely.

### Failure modes and provenance

An over-strict gate can discard a viable design; record the screening rule and
allow a reviewed bypass on a new plan. DC convergence does not prove stability,
noise or transient quality. Testbench/load changes between stages can invalidate
the apparent handoff: freeze variants and verify declared design equivalence.
Resume must not launch the next child after stale hashes, incomplete retrieval,
or an uncertain job. Record simulator version/options, rule bytes, metrics,
decisions, actual stage timings and all child input/result hashes.

### Tests and expected savings

Future implementation tests: fake-backend pass/fail gates; missing metric blocks;
execution failure distinct from rejection; changed predecessor hash rejects;
restart after each handoff; concurrent advances submit one child; optional stage
omissions never count as final coverage; remote cleanup remains required.

Assume costs DC=1, AC/STB=4, tran/noise=20 CPU-minutes; 50% pass DC and 40% of
those pass AC/STB. Full evaluation costs 25 per candidate; staged expectation is
`1 + 0.5*4 + 0.5*0.4*20 = 7`, a hypothetical 72% reduction before orchestration,
queue and extra startup overhead. If all candidates pass, savings are zero and
separate-job startup may increase cost. Measure conditional pass rates and
CPU-minutes; wall-clock savings depend on queue/concurrency, not this formula.

## B. Corner pruning and final full-corner confirmation

### Data model and state machine

The project owns a versioned complete corner set including operating conditions
and model-section hashes. Start exploratory evaluation at typical, then a
reviewed active set of historically binding corners. Record each metric's worst
normalized margin, binding corner, last complete coverage and audit age. If a
metric has two-sided limits, track both margins. A typical result or active-set
PASS is provisional and never a full-corner PASS.

Parent REVIEW can authorize pruning for exploration. A candidate nominated for
final decision enters proposed `CONFIRMING_CORNERS`, evaluates every required
corner at high fidelity, then returns to REVIEW. Only complete verified/analyzed
coverage allows CONFIRMED. Never infer monotonicity across corners. Rotate audit
corners during exploration; reset/review the active set when topology, parameter
region, models or rules change. Audit policy and cadence are project-owned.

### Failure modes and provenance

A previously nonbinding corner can become worst; pruning is a heuristic, not a
proof. Nonconvergent corners are unresolved, not ignored successes. Record all
skipped corner IDs and reasons, the complete corner-set hash, actual evaluated
conditions, child hashes, binding-margin calculations and audit decisions.
Final confirmation must reject stale or mismatched conditions/results. Statistical
yield/Monte Carlo claims require a separate design; corner coverage is not yield.

### Tests and expected savings

Future tests: synthetic worst-corner swaps; typical PASS/worst FAIL; two-sided
binding limits; missing/nonconvergent corner blocks confirmation; audit refresh;
restart mid-confirmation; changed corner-set hash invalidates coverage. Verify
that active-set comparisons are visibly provisional and excluded from a final
full-corner Pareto set.

Assume 10 equally costly corners, an active set of 3 including typical, and
20% of candidates nominated for full confirmation. Reusing already measured
active corners gives `3 + 0.2*(10-3) = 4.4` corner evaluations per candidate,
hypothetically 56% below 10. Add periodic audit cost separately. Unequal costs,
changing binding corners or many finalists reduce savings; full confirmation
itself saves nothing. Do not multiply this estimate by staged savings without
measuring correlated gate/corner selection rates.

## C. Clearly labelled low-fidelity screening

### Data model and state machine

Fidelity is an explicit, versioned identity, not a loose label. A project-private
definition declares solver tolerances, discretization, duration, model/detail
choices, metric semantics and expected bias. Its definition hash travels with
every child, metric row and coverage entry. Low fidelity still requires normal
simulation verification and analysis; verified means authentic execution, not
high-fidelity accuracy.

Maintain separate `screening_comparison` and `confirmation_comparison` views.
Low-fidelity points are never mixed into high-fidelity Pareto sets or observed
high-fidelity hypervolume. Promotion creates a new high-fidelity child with an
explicit parent/screening reference. A reviewed conservative screen can reject
an evaluation; screen PASS means PROMOTION_ELIGIBLE, not final feasibility.
The Phase 2 proposer currently has no fidelity contract: do not feed mixed
comparison rows into it; use separate campaigns until this extension exists.

### Failure modes and provenance

Bias may reorder candidates or hide instability/distortion/noise. Margins should
be calibrated on paired evaluations, with periodic high-fidelity audits of
screen-rejected points; disagreement disables or revises the screen through
review. Missing metric and execution failure remain separate from screen FAIL.
Record fidelity hash, paired IDs, errors by metric/region, promotion reason,
timing, solver/model hashes and high-fidelity confirmation evidence. Physical
rules for final decisions are evaluated on high-fidelity observations only.

### Tests and expected savings

Future tests: intentionally rank-reversing synthetic fidelities; strict Pareto/HV
separation; mismatched metric units reject pairs; promoted run cannot inherit
screen PASS; disagreement triggers review; low-fidelity failures never generate
invented high-fidelity objective values; restart and hash-tampering checks.

Assume low cost=1, high cost=20, 30% promotion: expected cost is `1+0.3*20=7`
versus 20, hypothetically 65% less. Audit promotions add cost. With all points
promoted cost=21, a 5% increase. Savings are unacceptable if screening loses the
best feasible design: report false-negative rate alongside cost, not cost alone.

## D. Cross-cycle reuse built on attach verification

### Data model and state machine

Current `campaign attach` accepts only a same-project VERIFIED, not-yet-analyzed
run, before new runs execute in the target cycle. It checks exact parameters and
testbench, public configuration, private manifests and netlist/deck/receipt
evidence. It is not a general cache of ANALYZED results. Extend this verifier
deliberately; never weaken it just because a cache key matches.

Propose a project-local, versioned reuse index mapping a SHA-256 key to immutable
source run IDs and handoff digests. Canonical numeric parameter fingerprints
use case-sensitive suffix evaluation (`1k` equals `1000`, `1M` differs from
`1m`); enum identities and testbench/stage/corner/fidelity are included. Include
public config, private-input hashes and declared-input hashes: generator, rules,
includes and dependency closure, metric plugins, models, stimulus/channel data,
tool versions/options and random seeds. Hash exact rendered deck bytes too.
Fail closed on undeclared/unknown dependencies. Private bytes/paths stay private;
public reports expose only necessary opaque hashes, never credentials.

A lookup produces a candidate reference, not permission to trust it. Proposed
`REUSE_CANDIDATE -> REUSE_VERIFIED -> REVIEW` attaches source evidence under a
campaign lock only after independently checking files and simulation receipts.
It creates no fabricated submission receipt and never mutates source artifacts.
Existing attach rejects string-different equivalent parameters; a future
canonical-key layer must retain original validated strings/deck hashes and
verify semantic equivalence instead of silently bypassing that check.

For ANALYZED reuse, extend the contract with source decision and analysis-result
hash checks. If only measurement policy changes, reanalyze verified preserved
data in a new analysis revision, preserving original evidence; never reuse a
stale verdict. Strict full-config hashing is the initial conservative policy:
even an irrelevant config change can cause a miss. Any later split into
simulation-input and analysis-input keys requires separately reviewed semantics.
Missing retained raw data makes reanalysis unavailable, not automatically safe.
Reuse charges no new simulation cost, but distinguishes logical design points,
new evaluations and historical observations so budgets/GPs do not double-count.

### Failure modes and provenance

Key collisions in poorly specified canonicalization, hidden model includes,
stale binaries, hash tampering, cleanup of required data and cross-project
references can all produce unsafe reuse. Require same-project scope initially,
path confinement, content verification, source-chain cycle detection and locking.
Index entries are hints rebuildable from immutable artifacts; a partial write
cannot turn an unverified reference into a hit. Record source run/cycle,
key/schema version, all input/result hashes, verifier version, decision and the
reason for each miss. Failed/uncertain execution is never reusable success.
Future stochastic runs require matching seeds and replicate semantics; an
identical seed result is not an independent replicate.

### Tests and expected savings

Future tests: equivalent units and M/m separation; changing every individual
input/private/tool/fidelity hash yields a miss; transitive include changes;
modified receipt/result rejects; cleaned source data; analysis-policy change;
concurrent lookup/attach; malicious paths; uncertain/failed source; duplicate
observation suppression; no simulator submission on verified hit.

Assume 25% exact verified hits, normal evaluation cost=20 and lookup/verification
cost=0.1 per request: expected cost `0.75*20+0.1=15.1`, hypothetically 24.5% less.
Storage/retention costs are excluded and must be measured. Strict configuration
hashes can reduce hit rate to zero, adding overhead. Reuse saves only repeated
identical evaluations, not new parameter exploration.

## E. Multi-fidelity extension of the Phase 2 proposer

### Data model and state machine

Add explicit fidelity features, per-evaluation cost and paired parameter IDs to
verified observation records. Continuous fidelity can use a multi-fidelity GP;
discrete model families can use task-indexed GPs. Separate GP outputs per objective
and constraint retain units and fidelity-specific likelihoods. Hard-failure
classification must condition on fidelity: low solver failure does not imply
high-fidelity physical infeasibility. Preserve failed/missing observations with
labels rather than imputing objective values.

Acquisition targets improvement of the high-fidelity feasible Pareto frontier
and values information gained from a lower-fidelity observation relative to its
cost, e.g. a cost-aware value-of-information design. Merely adding a fidelity
coordinate to qLogNEHVI is insufficient. Integrate uncertainty in fidelity bias
and use paired audits to validate correlation. Only actual high-fidelity complete
feasible observations define reported hypervolume and stop suggestions.

The proposer returns parameter AND requested-fidelity/stage recommendations plus
predicted high-fidelity objectives, feasibility, uncertainty, expected cost,
model/data hashes and acquisition value. This requires extending point-schema
validation first: Phase 2 `_validate_points` does not implement these fields.
Designer review still precedes campaign add; deterministic workers execute the
approved plan. Controller limits must bound both points and cumulative estimated
and actual cost; expensive mandatory final confirmations cannot be skipped when
budget runs out. Parent remains REVIEW/unconfirmed until coverage is complete.

### Failure modes and provenance

Weak/negative fidelity correlation, region-dependent bias, poorly estimated
costs or queue latency can make low-fidelity querying worse than high-only BO.
Disable the extension through review when held-out paired predictions fail
calibration; retain a high-only proposer as a replaceable baseline. Record
seed/library versions, model specification, paired observations, fidelity hashes,
posterior diagnostics, cost definition, acquisition approximation and all input
hashes. Determinism guarantees must be tested for the new acquisition separately.
Warm-start hyperparameters are optional, versioned state, not hidden global state.

### Tests and expected savings

Future tests: correlated, uncorrelated and rank-reversing synthetic fidelities;
hard failures per fidelity; high-only Pareto/HV invariant; paired calibration;
fixed-input determinism; finite costs and budget enforcement; review-only outputs;
replay and duplicate-source suppression. Benchmark high-only Phase 2 BO against
multi-fidelity BO at equal cumulative evaluation cost over at least five seeds,
including proposer overhead and cases where it loses.

Assume 40 high-fidelity calls would cost `40*20=800`; replacing the information
needed with 80 low calls and 20 high calls costs `80*1+20*20=480`, hypothetically
40% less, ONLY if equal high-fidelity decision quality is demonstrated. These
counts are an assumption, not a prediction or measured result. With poor
correlation, 80 low plus all 40 high costs 880, 10% more before GP overhead.

## Recommended order

1. Define the parent coverage schema, immutable provenance and cost accounting;
   extend verified attach/reuse conservatively, with tamper and race tests.
2. Add opt-in staged gates and recovery before any approximation or pruning.
3. Add typical-first/binding-corner exploration with mandatory full confirmation.
4. Introduce explicitly labelled low-fidelity screens and paired audits; keep
   comparisons/Pareto sets separated even without multi-fidelity BO.
5. Evaluate a replaceable multi-fidelity proposer only after calibration and
   equal-cost benchmarks justify its added complexity.

Each implementation task needs its own commit, unit/fake-backend tests and review;
default legacy behavior remains unchanged. Phase 3 supplies none of these runtime
features. Do not add the illustrative fields to an existing project expecting
them to work today. Expected savings above are not additive and not measured
circuit results; report CPU/license usage, queue and wall time, fidelity error,
missed feasible candidates and final coverage separately.

## Open questions

- Which gates can a reviewed plan preauthorize, and which require an explicit
  per-stage decision? Who owns override/bypass approval and audit cadence?
- What is the complete model/include dependency closure, and can each backend
  attest tool/version hashes without exposing private paths or license secrets?
- What raw-data retention is affordable for reuse/reanalysis, and how should
  existing cleanup policies report that a source is no longer reusable?
- Should cycle cost budgets count retries, cache hits, queue time and final
  confirmations separately? How are budget overruns approved rather than hidden?
- Which observed mismatch disables screening or corner pruning, and how often
  must rejected designs receive high-fidelity audit?
- Which fidelity families are correlated enough for a joint GP, and how should
  mixed enums, stochastic replicates and region-specific bias be represented?
- Can evaluation plans stay same-project and fail-closed initially, or is a
  separately authorized cross-project trust model eventually needed?
