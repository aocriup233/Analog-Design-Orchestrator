"""Deterministic constrained MOBO proposals; never writes files or runs jobs.

Heavy dependencies are imported only when proposing. See docs/mobo.md for the
categorical representation, cold-start priors and failure classifier semantics.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ..core import load_config, local_path, read_json
from ..parameters import number_value, validate_value, validate_values


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


class Space:
    """Bounded numbers and one-hot enums; category combinations are enumerated."""
    def __init__(self, definitions: dict):
        if not definitions:
            raise ValueError("MOBO needs at least one declared parameter")
        self.definitions = definitions
        self.fields = []
        self.width = 0
        for name, schema in definitions.items():
            if schema.get("type") == "enum":
                candidates = schema.get("candidates")
                if not isinstance(candidates, list) or not candidates:
                    raise ValueError(f"Enum {name} needs candidates")
                choices = [validate_value(name, item, schema) for item in candidates]
                if len(set(choices)) != len(choices):
                    raise ValueError(f"Enum {name} has duplicate candidates")
                self.fields.append((name, self.width, choices, None, None))
                self.width += len(choices)
            else:
                if schema.get("type") not in (None, "number") or not {"min", "max"} <= schema.keys():
                    raise ValueError(f"Numeric parameter {name} requires finite min and max bounds")
                lower, upper = number_value(str(schema["min"])), number_value(str(schema["max"]))
                if lower >= upper or (schema.get("log") and lower <= 0):
                    raise ValueError(f"Invalid bounds for {name}; log bounds must be positive")
                self.fields.append((name, self.width, None, lower, upper))
                self.width += 1

    def values(self, parameters):
        unknown = parameters.keys() - self.definitions.keys()
        if unknown:
            raise ValueError(f"Unknown parameters: {sorted(unknown)}")
        values = {name: parameters.get(name, schema.get("initial"))
                  for name, schema in self.definitions.items()}
        return validate_values(values, self.definitions)

    def encode(self, parameters):
        values = self.values(parameters)
        result = []
        for name, _, choices, lower, upper in self.fields:
            if choices is not None:
                result.extend(float(values[name] == choice) for choice in choices)
            else:
                value = number_value(values[name])
                if not lower <= value <= upper:
                    raise ValueError(f"Historical parameter {name} is outside current bounds")
                if self.definitions[name].get("log"):
                    value, lower, upper = math.log(value), math.log(lower), math.log(upper)
                result.append((value - lower) / (upper - lower))
        return result

    def decode(self, coordinates):
        result = {}
        for name, offset, choices, lower, upper in self.fields:
            if choices is not None:
                result[name] = choices[max(range(len(choices)), key=lambda i: coordinates[offset + i])]
            else:
                fraction = min(1.0, max(0.0, float(coordinates[offset])))
                if fraction == 0:
                    value = lower
                elif fraction == 1:
                    value = upper
                elif self.definitions[name].get("log"):
                    value = math.exp(math.log(lower) + fraction * (math.log(upper) - math.log(lower)))
                else:
                    value = lower + fraction * (upper - lower)
                result[name] = format(min(upper, max(lower, value)), ".17g")
        return validate_values(result, self.definitions)

    def key(self, parameters):
        # Numerical equivalence: 1k and 1000 are the same point.
        values = self.values(parameters)
        return tuple(values[name] if choices is not None else number_value(values[name])
                     for name, _, choices, _, _ in self.fields)

    def fixed_features(self, limit):
        categories = []
        for _, offset, choices, _, _ in self.fields:
            if choices is not None:
                categories.append([{offset + i: float(i == selected) for i in range(len(choices))}
                                   for selected in range(len(choices))])
        count = math.prod(len(items) for items in categories)
        if count > limit:
            raise ValueError(f"Enum combinations ({count}) exceed bo.max_enum_combinations ({limit})")
        return [dict(itertools.chain.from_iterable(item.items() for item in combination))
                for combination in itertools.product(*categories)] if categories else []


def hypervolume(points: list[list[float]], reference: list[float]) -> float:
    """Exact union volume for maximization; finite observed feasible points only."""
    points = [point for point in points if all(a > b for a, b in zip(point, reference))]
    if not points:
        return 0.0
    if len(reference) == 1:
        return max(point[0] for point in points) - reference[0]
    edges = sorted({reference[0], *(point[0] for point in points)})
    return sum((right - left) * hypervolume([p[1:] for p in points if p[0] >= right], reference[1:])
               for left, right in zip(edges, edges[1:]))


def _history(campaign: Path, space: Space, required: list[str], rules: dict):
    rows, cycles, hashes, excluded = [], [], {}, []
    for file in sorted(campaign.glob("cycle-*_comparison.json")):
        if not re.fullmatch(r"cycle-\d{4,}_comparison\.json", file.name):
            continue
        content = local_path(campaign, file.name).read_bytes()
        hashes[file.name] = hashlib.sha256(content).hexdigest()
        comparison = json.loads(content)
        batch = []
        for index, point in enumerate(comparison.get("points", [])):
            # Validate syntax/types independently of current numeric bounds.
            # Only bound changes are tolerated, not malformed historical values.
            relaxed = {name: {key: value for key, value in schema.items() if key not in {"min", "max"}}
                       for name, schema in space.definitions.items()}
            raw = point["parameters"]
            parameters = validate_values({name: raw.get(name, schema.get("initial"))
                                         for name, schema in space.definitions.items()}, relaxed)
            if raw.keys() - space.definitions.keys():
                raise ValueError("Unknown historical parameters")
            outside = [name for name, _, choices, lower, upper in space.fields
                       if choices is None and not lower <= number_value(parameters[name]) <= upper]
            if outside:
                excluded.append({"comparison": file.name, "point_index": index,
                                 "point_id": point.get("id"), "parameters": parameters,
                                 "reason": "outside current numeric bounds", "fields": outside})
                continue
            x = space.encode(parameters)
            metrics = {}
            measurements = point.get("metrics")
            for name, value in (measurements if isinstance(measurements, dict) else {}).items():
                if isinstance(value, bool):
                    continue
                try:
                    numeric = float(value)
                except (ValueError, TypeError):
                    continue
                if math.isfinite(numeric):
                    metrics[name] = numeric
            complete = all(name in metrics for name in required)
            verdict = point.get("verdict")
            measured = complete and verdict in {"PASS", "FAIL"} and point.get("state") != "FAILED"
            passing_rules = complete and all(
                ("min" not in rule or metrics[name] >= float(rule["min"]))
                and ("max" not in rule or metrics[name] <= float(rule["max"]))
                for name, rule in rules.items())
            feasible = measured and verdict == "PASS" and passing_rules
            # A FAIL with violated measured rules is explained by constraint GPs.
            # Hard failures / missing measurements / unexplained verdicts feed the classifier.
            hard_valid = measured and (feasible or (verdict == "FAIL" and not passing_rules))
            row = {"parameters": parameters, "x": x, "metrics": metrics,
                   "valid": measured, "feasible": feasible, "hard_valid": hard_valid}
            rows.append(row)
            batch.append(row)
        cycles.append(batch)
    return rows, cycles, hashes, excluded


def _reference(names, directions, rules, bo):
    configured = bo.get("ref_point")
    if configured is None:
        configured = {name: rules.get(name, {}).get("max" if directions[name] == "min" else "min")
                      for name in names}
    if isinstance(configured, dict):
        configured = [configured.get(name) for name in names]
    if not isinstance(configured, list) or len(configured) != len(names):
        raise ValueError("bo.ref_point must map each objective or list values in metric_directions order")
    try:
        values = [float(value) for value in configured]
    except (ValueError, TypeError) as error:
        raise ValueError("Each objective needs a rule threshold or explicit bo.ref_point") from error
    if not all(math.isfinite(value) for value in values):
        raise ValueError("bo.ref_point must be finite")
    return [value * (-1 if directions[name] == "min" else 1) for name, value in zip(names, values)]


def _versions():
    result = {}
    for name in ("torch", "gpytorch", "botorch", "scipy"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def _sobol(space, seed, count, offset=0):
    from scipy.stats import qmc
    engine = qmc.Sobol(d=len(space.fields), scramble=True, seed=seed)
    if offset:
        engine.fast_forward(offset)
    # Draw one at a time: any batch size is legal, without a power-of-two warning.
    result = []
    for _ in range(count):
        sample = engine.random(1)[0]
        coordinates = []
        for fraction, (_, _, choices, _, _) in zip(sample, space.fields):
            if choices is None:
                coordinates.append(float(fraction))
            else:
                selected = min(len(choices) - 1, int(fraction * len(choices)))
                coordinates.extend(float(i == selected) for i in range(len(choices)))
        result.append(space.decode(coordinates))
    return result


def propose(context: dict) -> dict:
    """Read all comparisons and return a deterministic, reviewed-only batch."""
    cfg = load_config(Path(context["project_config"]))
    root = Path(cfg["_project_root"])
    campaign_id = context["campaign_id"]
    if not isinstance(campaign_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", campaign_id):
        raise ValueError("Invalid campaign_id")
    campaign = local_path(root, "campaigns/" + campaign_id)
    settings = cfg.get("campaign", {})
    bo = settings.get("bo", {})
    directions = settings.get("metric_directions", {})
    if len(directions) < 2 or any(value not in {"min", "max"} for value in directions.values()):
        raise ValueError("MOBO requires at least two metric_directions (min/max)")
    names = list(directions)
    rules = {name: rule for name, rule in cfg.get("rules", {}).items() if "min" in rule or "max" in rule}
    required = list(dict.fromkeys([*names, *rules]))
    space = Space(cfg.get("parameters", {}))
    fixed = space.fixed_features(_positive(bo.get("max_enum_combinations", 128), "bo.max_enum_combinations"))
    rows, cycles, hashes, excluded = _history(campaign, space, required, rules)
    manifest_path = campaign / "campaign.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else None
    policy = context.get("policy", {})
    used = sum(len(cycle["points"]) for cycle in manifest["cycles"]) if manifest else len(rows) + len(excluded)
    remaining = _positive(policy.get("max_total_points", settings.get("max_total_points", 200)), "max_total_points") - used
    if remaining <= 0:
        raise ValueError("Campaign has no remaining point budget")
    if len(cycles) >= policy.get("max_cycles", settings.get("max_cycles", 20)):
        raise ValueError("Campaign reached max_cycles")
    q = min(_positive(bo.get("batch_size", policy.get("max_parallel", settings.get("max_parallel", 1))), "bo.batch_size"),
            _positive(policy.get("max_points_per_cycle", settings.get("max_points_per_cycle", 32)), "max_points_per_cycle"), remaining)
    seed = bo.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("bo.seed must be an integer in [0, 2**32)")
    reference = _reference(names, directions, rules, bo)
    signs = [-1 if directions[name] == "min" else 1 for name in names]
    cumulative, hv_history = [], []
    for cycle in cycles:
        cumulative.extend([[row["metrics"][name] * sign for name, sign in zip(names, signs)]
                           for row in cycle if row["feasible"]])
        hv_history.append(hypervolume(cumulative, reference))
    patience = _positive(bo.get("patience", 3), "bo.patience")
    eps = float(bo.get("hv_eps", 1e-6))
    if not math.isfinite(eps) or eps < 0:
        raise ValueError("bo.hv_eps must be finite and nonnegative")
    improvements = [right - left for left, right in zip([0.0, *hv_history], hv_history)]
    stop = len(improvements) >= patience and all(value < eps for value in improvements[-patience:])
    cold = sum(row["valid"] for row in rows) < _positive(bo.get("n_init", 2 * len(space.fields) + 1), "bo.n_init")
    seen = {space.key(row["parameters"]) for row in rows}
    seed_points = []
    for item in bo.get("seed_points", []):
        values = space.values(item.get("parameters", item))
        space.encode(values)
        key = space.key(values)
        if key not in seen and key not in {space.key(point) for point in seed_points}:
            seed_points.append(values)
    meta = {"seed": seed, "library_versions": _versions(), "data_count": len(rows),
            "history_count": len(rows) + len(excluded), "excluded_history": excluded,
            "valid_count": sum(row["valid"] for row in rows),
            "infeasible_count": sum(not row["feasible"] for row in rows),
            "hard_failure_count": sum(not row["hard_valid"] for row in rows),
            "current_hypervolume": hv_history[-1] if hv_history else 0.0,
            "hypervolume_history": hv_history, "stop_suggestion": stop,
            "comparison_sha256": hashes, "ref_point_maximization": reference,
            "config_sha256": hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest(),
            "mode": "cold_start" if cold else "mobo", "batch_size": q,
            "enum_strategy": "enumerated one-hot", "review_required": True}
    threshold = float(bo.get("failure_warning_threshold", 0.5))
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("bo.failure_warning_threshold must be in [0, 1]")
    meta["failure_rate"] = meta["hard_failure_count"] / len(rows) if rows else 0.0
    meta["warnings"] = (["High hard-failure rate; inspect netlist and simulation configuration before further sampling."]
                        if rows and meta["failure_rate"] > threshold else [])
    try:
        if cold:
            candidates = seed_points[:q]
            for point in _sobol(space, seed, max(256, q * 32), len(rows)):
                if len(candidates) >= q:
                    break
                key = space.key(point)
                if key not in seen and key not in {space.key(item) for item in candidates}:
                    candidates.append(point)
            summaries = []
            for name in names:
                observed = [row["metrics"][name] for row in rows if name in row["metrics"]]
                mean = sum(observed) / len(observed) if observed else 0.0
                std = math.sqrt(sum((value - mean)**2 for value in observed) / max(1, len(observed) - 1)) if len(observed) > 1 else 1.0
                summaries.append((mean, max(std, 1e-12)))
            # Explicitly untrained priors, not fabricated GP predictions.
            probability = (1 + sum(row["feasible"] for row in rows)) / (2 + len(rows))
            details = [(summaries, probability, 0.0, "untrained empirical prior; Sobol/seed acquisition placeholder") for _ in candidates]
            meta["acquisition"] = "scrambled_sobol"
        else:
            candidates, details, model_meta = _bayesian(space, rows, names, signs, rules, reference, bo, seed, q, fixed, seen)
            meta.update(model_meta)
    except ImportError as error:
        raise ImportError("MOBO needs the optional BO dependencies: pip install 'analog-agent[bo]'") from error
    if len(candidates) != q:
        raise ValueError("Cannot form a unique batch; the discrete space may be exhausted")
    points = []
    for index, (parameters, (predictions, probability, acquisition, label)) in enumerate(zip(candidates, details), 1):
        rationale = "; ".join(f"{name}: predicted mean {mean:.8g} ± std {std:.8g}" for name, (mean, std) in zip(names, predictions))
        rationale += f"; feasibility probability {probability:.8g}; acquisition value {acquisition:.8g}; {label}"
        points.append({"id": f"bo-{used + index:06d}", "parameters": parameters, "rationale": rationale})
    return {"points": points, "meta": meta}


def _bayesian(space, rows, names, signs, rules, reference, bo, seed, q, fixed, seen):
    # Lazy import: lean workers and test discovery do not require this stack.
    import numpy as np
    import torch
    from botorch.models import SingleTaskGP, ModelListGP
    from botorch.models.approximate_gp import SingleTaskVariationalGP
    from botorch.models.transforms.outcome import Standardize
    from botorch.fit import fit_gpytorch_mll
    from botorch.optim import optimize_acqf, optimize_acqf_mixed
    from botorch.sampling.normal import SobolQMCNormalSampler
    from botorch.acquisition import AcquisitionFunction
    from botorch.acquisition.multi_objective.objective import GenericMCMultiOutputObjective
    from gpytorch.likelihoods import BernoulliLikelihood
    from gpytorch.mlls import ExactMarginalLogLikelihood, VariationalELBO
    try:
        from botorch.acquisition.multi_objective.logei import qLogNoisyExpectedHypervolumeImprovement as Acquisition
        logarithmic = True
    except ImportError:
        from botorch.acquisition.multi_objective.monte_carlo import qNoisyExpectedHypervolumeImprovement as Acquisition
        logarithmic = False
    numpy_state, threads = np.random.get_state(), torch.get_num_threads()
    deterministic = torch.are_deterministic_algorithms_enabled()
    try:
        np.random.seed(seed)
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(seed)
            tensor = lambda data: torch.tensor(data, dtype=torch.double, device="cpu")
            metric_names = list(dict.fromkeys([*names, *rules]))
            models = []
            for name in metric_names:
                observed = [row for row in rows if name in row["metrics"]]
                if not observed:
                    raise ValueError(f"No observed values for GP metric {name}")
                model = SingleTaskGP(tensor([row["x"] for row in observed]),
                                     tensor([[row["metrics"][name]] for row in observed]),
                                     outcome_transform=Standardize(m=1))
                fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model),
                                 optimizer_kwargs={"options": {"maxiter": _positive(bo.get("fit_maxiter", 80), "bo.fit_maxiter")}})
                model.eval()
                models.append(model)
            model = ModelListGP(*models)
            all_x = tensor([row["x"] for row in rows])
            classifier = None
            if any(not row["hard_valid"] for row in rows):
                classifier = SingleTaskVariationalGP(all_x, likelihood=BernoulliLikelihood(),
                                                     inducing_points=all_x[:min(64, len(rows))],
                                                     outcome_transform=None).double()
                classifier.train()
                labels = tensor([float(row["hard_valid"]) for row in rows])
                optimizer = torch.optim.Adam(classifier.parameters(), lr=0.05)
                elbo = VariationalELBO(classifier.likelihood, classifier.model, num_data=len(rows))
                for _ in range(_positive(bo.get("classifier_steps", 80), "bo.classifier_steps")):
                    optimizer.zero_grad()
                    loss = -elbo(classifier.model(all_x), labels)
                    loss.backward()
                    optimizer.step()
                classifier.eval()
                classifier.likelihood.eval()
            constraints = []
            for name, rule in rules.items():
                column = metric_names.index(name)
                if "min" in rule:
                    constraints.append(lambda samples, column=column, bound=float(rule["min"]): bound - samples[..., column])
                if "max" in rule:
                    constraints.append(lambda samples, column=column, bound=float(rule["max"]): samples[..., column] - bound)
            sampler = SobolQMCNormalSampler(torch.Size([_positive(bo.get("mc_samples", 64), "bo.mc_samples")]), seed=seed)
            objective = GenericMCMultiOutputObjective(lambda samples, X=None: samples[..., :len(names)] * tensor(signs))
            baseline = tensor([row["x"] for row in rows if row["feasible"]]).reshape(-1, space.width)
            feasibility_first = baseline.shape[0] == 0
            base = None if feasibility_first else Acquisition(model=model, ref_point=reference, X_baseline=baseline,
                               sampler=sampler, objective=objective, constraints=constraints or None,
                               prune_baseline=False, cache_root=False)

            class FailureWeighted(AcquisitionFunction):
                def __init__(self):
                    super().__init__(model=model)
                    self.base = base
                def forward(self, X):
                    if feasibility_first:
                        posterior = model.posterior(X)
                        mean = posterior.mean
                        std = posterior.variance.clamp_min(1e-18).sqrt()
                        normal = torch.distributions.Normal(tensor(0.0), tensor(1.0))
                        probability = torch.ones_like(mean[..., 0])
                        for name, rule in rules.items():
                            column = metric_names.index(name)
                            upper = normal.cdf((float(rule["max"]) - mean[..., column]) / std[..., column]) if "max" in rule else 1.0
                            lower = normal.cdf((float(rule["min"]) - mean[..., column]) / std[..., column]) if "min" in rule else 0.0
                            probability = probability * (upper - lower).clamp(0, 1)
                        if classifier is not None:
                            probability = probability * classifier.likelihood(classifier.model(X)).mean
                        return probability.prod(dim=-1)
                    value = self.base(X)
                    if classifier is not None:
                        probability = classifier.likelihood(classifier.model(X)).mean.clamp(1e-9, 1 - 1e-9)
                        weight = probability.prod(dim=-1)
                        return value + weight.log() if logarithmic else value * weight
                    return value
                def set_X_pending(self, X_pending=None):
                    super().set_X_pending(X_pending)
                    if self.base is not None:
                        self.base.set_X_pending(X_pending)

            acquisition = FailureWeighted()
            bounds = torch.stack([torch.zeros(space.width, dtype=torch.double), torch.ones(space.width, dtype=torch.double)])
            candidates, details, encoded = [], [], []
            for _ in range(q):
                if encoded:
                    acquisition.set_X_pending(tensor(encoded))
                kwargs = {"acq_function": acquisition, "bounds": bounds, "q": 1,
                          "num_restarts": _positive(bo.get("num_restarts", 4), "bo.num_restarts"),
                          "raw_samples": _positive(bo.get("raw_samples", 64), "bo.raw_samples"),
                          "options": {"maxiter": _positive(bo.get("acq_maxiter", 80), "bo.acq_maxiter")}}
                result, _value = optimize_acqf_mixed(**kwargs, fixed_features_list=fixed) if fixed else optimize_acqf(**kwargs)
                parameters = space.decode(result[0].detach().tolist())
                # A finite discrete space or a boundary optimum can repeat a point.
                # Score a deterministic legal Sobol pool as an explicit fallback.
                if space.key(parameters) in seen or space.key(parameters) in {space.key(item) for item in candidates}:
                    pool = [item for item in _sobol(space, seed, 256, len(rows) + len(candidates))
                            if space.key(item) not in seen and space.key(item) not in {space.key(point) for point in candidates}]
                    if not pool:
                        break
                    with torch.no_grad():
                        scores = acquisition(tensor([space.encode(item) for item in pool]).unsqueeze(1))
                    parameters = pool[int(scores.argmax())]
                x = tensor([space.encode(parameters)])
                with torch.no_grad():
                    posterior = model.posterior(x)
                    mean = posterior.mean[0]
                    std = posterior.variance[0].clamp_min(1e-18).sqrt()
                    probability = 1.0
                    normal = torch.distributions.Normal(tensor(0.0), tensor(1.0))
                    for name, rule in rules.items():
                        column = metric_names.index(name)
                        upper = normal.cdf((tensor(float(rule["max"])) - mean[column]) / std[column]) if "max" in rule else tensor(1.0)
                        lower = normal.cdf((tensor(float(rule["min"])) - mean[column]) / std[column]) if "min" in rule else tensor(0.0)
                        probability *= float((upper - lower).clamp(0, 1))
                    if classifier is not None:
                        probability *= float(classifier.likelihood(classifier.model(x)).mean[0])
                    value = float(acquisition(x.unsqueeze(0)).reshape(-1)[0])
                if not math.isfinite(value):
                    raise ValueError("Acquisition returned a non-finite value")
                candidates.append(parameters)
                encoded.append(space.encode(parameters))
                details.append(([(float(mean[i]), float(std[i])) for i in range(len(names))], probability, value,
                                "GP posterior; feasibility-first acquisition" if feasibility_first else
                                "GP posterior; log acquisition" if logarithmic else "GP posterior; acquisition"))
            return candidates, details, {"acquisition": "probability_of_feasibility" if feasibility_first else "qLogNEHVI" if logarithmic else "qNEHVI",
                                         "gp_metric_names": metric_names,
                                         "gp_observation_counts": {name: sum(name in row["metrics"] for row in rows) for name in metric_names},
                                         "classifier_observation_count": len(rows) if classifier is not None else 0}
    finally:
        np.random.set_state(numpy_state)
        torch.set_num_threads(threads)
        torch.use_deterministic_algorithms(deterministic)
