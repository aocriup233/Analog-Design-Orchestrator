# Optional constrained multi-objective proposer

Install `python -m pip install -e '.[bo]'`. Core workers and importing the
proposer do not import Torch, GPyTorch, BoTorch or SciPy. Cold-start proposals
need SciPy; GP proposals need the complete extra. Missing dependencies produce
an installation hint. No simulator is required to test the proposer.

## Configuration and review boundary

This generic example assumes a project-owned `design.scs` exists. Metric names,
bounds, thresholds and reference points are design choices, not ADO defaults.

```json
{
  "template": "design.scs",
  "parameters": {
    "x": {"type": "number", "initial": "0.5", "min": 0, "max": 1},
    "scale": {"type": "number", "initial": "1", "min": "1m", "max": "1M", "log": true},
    "mode": {"type": "enum", "initial": "fast", "candidates": ["fast", "slow"]}
  },
  "metrics": {
    "objective_a": {"kind": "scalar", "key": "objective_a"},
    "objective_b": {"kind": "scalar", "key": "objective_b"},
    "constraint_a": {"kind": "scalar", "key": "constraint_a"}
  },
  "rules": {"constraint_a": {"max": 0}},
  "campaign": {
    "proposer": "analog_agent.proposers.mobo:propose",
    "metric_directions": {"objective_a": "min", "objective_b": "max"},
    "max_parallel": 4,
    "max_points_per_cycle": 8,
    "max_total_points": 100,
    "bo": {
      "seed": 0,
      "batch_size": 4,
      "ref_point": {"objective_a": 10, "objective_b": -10},
      "seed_points": [{"x": "0.2", "scale": "1", "mode": "fast"}],
      "hv_eps": 0.000001,
      "patience": 3
    }
  }
}
```

Call `analog-agent campaign propose CAMPAIGN_DIR`. Review the saved JSON, then
explicitly call `campaign add CAMPAIGN_DIR PROPOSAL.json`. The proposer is a
read-only function: it never creates runs, submits simulations, mutates history,
or accepts its own stop suggestion. `campaign.propose` holds the campaign lock,
validates the result and writes the proposal through the existing JSON contract.

## Data and feasibility

`propose(context)` uses `context.project_config` and `context.campaign_id`. It
loads every sorted `cycle-XXXX_comparison.json` in the campaign directory, not
just `latest_comparison`. Metadata records SHA-256 hashes of those exact bytes
and a canonical hash of the effective config. It does not read run directories.

Parameter values use the Phase 1 validator, including Spectre's case-sensitive
scale factors (`M` is mega; `m` is milli). Out-of-space historical parameters are
an error, rather than silently removed. Keep parameter and metric definitions
consistent across the comparisons; topology/fidelity changes require a separate
campaign until compatible provenance handling is explicitly implemented.

Objectives are the metrics in `campaign.metric_directions`. Every configured
metric min/max rule is an outcome constraint, even when it is not an objective.
An observed Pareto/HV point must have `verdict=PASS`, finite values for all required
metrics, no FAILED state, and satisfy the recorded thresholds. A conflicting
PASS is conservatively infeasible. Missing/nonfinite metrics are not imputed.

Each objective/constraint metric GP uses **all available finite measurements**
for that metric, including available measurements at otherwise incomplete points.
FAILED and incomplete rows remain in the observation count, deduplication,
budget accounting and binary classifier training. They never become measured
Pareto points. A complete FAIL explained by a measured rule violation is modeled
by the constraint GP. Hard failures, incomplete rows and unexplained/conflicting
verdicts have binary label 0; successfully measured/explained rows have label 1.

## Space and models

Every numeric parameter needs finite `min < max`; unbounded numeric spaces fail
with a parameter-specific message. `log: true` requires positive bounds and
normalizes log values. Numeric `candidates` remain designer suggestions, not an
implicit grid. Values are emitted as validated strings (17 significant digits).

Enums use one-hot encoding with **enumeration of legal category combinations**,
not ordinal encoding or rounding a continuous category. Mixed acquisition
optimization fixes the one-hot coordinates for each combination. The default
`bo.max_enum_combinations=128` prevents accidental exponential work; exceeding
it raises an error. This is appropriate for small categorical spaces, not a
claim of scalability to hundreds of independent category combinations.

One `SingleTaskGP` per objective and constraint metric is combined in a
`ModelListGP`; outputs are standardized and inputs are normalized. When hard
failure observations exist, a `SingleTaskVariationalGP` with Bernoulli likelihood
and variational ELBO is fitted to **all** binary observations. No arbitrary
failure penalty is inserted into a missing objective.

## Acquisition and cold start

Use qLogNEHVI when available, otherwise qNEHVI. Constraints operate in raw
metric units, with `g(sample) <= 0` as feasible. Minimization objectives are
negated internally. The reference point is either `bo.ref_point` (raw units,
dict or list in metric-direction order), or a rule max for each minimized
objective / rule min for each maximized objective. Missing thresholds without
an explicit reference point are an error.

The observed baseline contains only confirmed feasible points; an empty
baseline is supported. Batches use greedy pending-point qNEHVI selection. When
the optimizer repeats an existing point, a deterministic legal Sobol pool is
scored as a fallback. Hard-failure probability weights the acquisition outside
the outcome-constraint estimator: log probability is added to qLogNEHVI, or
probability multiplies qNEHVI. This independent feasibility weighting is an
approximation, not a joint correlated objective/failure model. Reported
feasibility multiplies metric marginal constraint probabilities and the binary
classifier probability; it is not a calibrated reliability guarantee.

With fewer than `bo.n_init` fully measured PASS/FAIL observations (default
`2*d+1`, where d is the declared parameter count), cold start takes unseen
`bo.seed_points` first, then fills the batch with scrambled Sobol points. Seeds
can contain a direct parameter object or a `parameters` object. Designers may
supply their own gm/Id-derived seed points; public code contains no such rules.
Numerically equivalent values (`1k` and `1000`) deduplicate against all history,
including failures, and within a batch. Excess seed points wait for later calls.

Cold-start rationales explicitly report **untrained empirical priors**, not GP
predictions: mean/sample std from finite values, or mean=0/std=1 without data;
the feasibility prior is Beta(1+PASS, 1+other), and acquisition=0 is an explicit
Sobol placeholder. Warm rationales report GP mean ± std, estimated feasibility
and log/non-log acquisition value. An exhausted discrete space raises instead
of manufacturing duplicate evaluations.

`q = min(bo.batch_size or max_parallel, max_points_per_cycle, remaining budget)`.
The actual campaign manifest counts all allocated points, including pending or
failed ones; standalone fixtures without a manifest count comparison rows.
Reaching point/cycle limits is an error. Stop suggestions never change this rule.

## Determinism, metadata and tuning

When warm-start history has no feasible point, acquisition explicitly maximizes
probability of feasibility: Gaussian rule-interval probabilities multiplied by
the hard-failure classifier probability (an independence approximation).
It returns to constrained hypervolume acquisition once a feasible point exists.
This mode has no pending-point repulsion; numerical deduplication and the scored
Sobol fallback still prevent duplicate evaluations. Cold starts remain Sobol.
Points exactly on the default reference thresholds contribute zero hypervolume.

Numeric history outside tightened bounds is excluded from models and current
hypervolume, with comparison/point identity, parameters and reason recorded in
`meta.excluded_history`. `history_count` and the point budget still include it.
Malformed values, unknown parameters and incompatible enum/schema changes remain
errors; this is not permission to mix different circuit or metric semantics.

`meta.failure_rate` is the hard-failure fraction of retained history (not measured
specification failures). Above `bo.failure_warning_threshold` (default 0.5,
range [0,1]), `meta.warnings` recommends checking netlist/simulation configuration.
Empty retained history has rate zero. The warning is advisory: it does not submit,
stop a campaign, or silently replace cold-start Sobol with a trained model.

Fixed `bo.seed` (default 0), CPU double precision, one Torch thread and deterministic
Torch operations provide repeatable proposal JSON bytes for identical input
artifacts, paths, dependency versions and hardware. RNG states/thread settings
are restored on return. This is not a cross-version or cross-platform bitwise
guarantee. Use separate proposer processes for concurrent campaigns; Torch and
NumPy process-global settings are not a thread-safe plugin interface.

The proposal has no timestamps. Its `meta` includes seed, library versions,
total/valid/infeasible/hard-failure counts, current and cumulative per-cycle HV,
reference point, config/comparison hashes, acquisition and GP/classifier counts.
Campaign proposal filenames remain UUID-based for compatibility, but repeated
identical proposals have identical **contents**, tested through the real hook.

`stop_suggestion=true` after `bo.patience` cycles whose absolute feasible observed
HV improvements are each strictly below `bo.hv_eps`. HV units depend on objectives
and the fixed reference point; choose epsilon accordingly. A sequence of failures
can trigger this suggestion without finding a usable design: inspect counts and
comparisons before stopping. No automatic stopping or next cycle is implemented.

Performance knobs (all positive integers): `fit_maxiter=80`, `acq_maxiter=80`,
`num_restarts=4`, `raw_samples=64`, `mc_samples=64`, `classifier_steps=80`.
Lower values save proposer CPU time but can reduce fit/search quality. GP fitting
is cubic in the number of observations; this initial implementation refits each
proposal and does not offer TuRBO, online model caches or multi-fidelity modeling.
BoTorch numerical/optimization warnings must be inspected; optimizer retries do
not mean another simulator evaluation. Some BoTorch versions optionally compile
a C++ accelerator; without a compiler they fall back to Python.

References: [BoTorch multi-objective guidance](https://botorch.org/docs/v0.16.0/multi_objective),
[constrained multi-objective tutorial](https://botorch.org/docs/v0.13.0/tutorials/constrained_multi_objective_bo),
[model API](https://botorch.readthedocs.io/en/stable/models.html).
See [the honest five-seed benchmark](bench_mobo.md) for measured results and limits.
