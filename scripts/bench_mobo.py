"""Reproducible equal-budget synthetic benchmark; no simulator or ADO runs.

Run from the repository with PYTHONPATH=src. Benchmark-only dependencies are
pymoo and matplotlib, in addition to the optional [bo] stack.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

from analog_agent.core import write_json
from analog_agent.proposers.mobo import hypervolume, propose

REFERENCE = [1.1, 11.0]  # Raw minimization units, shared across all methods.
METHODS = ("random", "nsga2", "mobo")


def evaluate(values):
    """ZDT1-like trade-off with x0 + mean(x1:) / 4 <= .85."""
    x = [float(value) for value in values]
    tail = sum(x[1:]) / (len(x) - 1)
    scale = 1 + 9 * tail
    return {"f1": x[0], "f2": scale * (1 - math.sqrt(x[0] / scale)),
            "constraint": x[0] + tail / 4 - .85}


def row(values):
    metrics = evaluate(values)
    return {"parameters": {f"x{i}": format(float(value), ".17g") for i, value in enumerate(values)},
            "metrics": metrics, "verdict": "PASS" if metrics["constraint"] <= 0 else "FAIL"}


def hv_curve(rows):
    feasible, curve = [], []
    for point in rows:
        if point["verdict"] == "PASS":
            feasible.append([-point["metrics"]["f1"], -point["metrics"]["f2"]])
        curve.append(hypervolume(feasible, [-value for value in REFERENCE]))
    return curve


def initial_design(dimension, seed, batch_size=4):
    from scipy.stats import qmc
    count = math.ceil((2 * dimension + 1) / batch_size) * batch_size
    engine = qmc.Sobol(dimension, scramble=True, seed=seed)
    return engine.random_base2(math.ceil(math.log2(count)))[:count]


def benchmark(method, dimension, seed, budget, bo_settings=None):
    import numpy as np
    initial = initial_design(dimension, seed)
    if budget < len(initial) or budget % len(initial):
        raise ValueError("Budget must be a multiple of the shared initial/population size")
    started = time.perf_counter()
    proposals = []
    if method == "random":
        rng = np.random.default_rng(seed)
        rows = [row(values) for values in initial]
        rows.extend(row(values) for values in rng.random((budget - len(initial), dimension)))
    elif method == "nsga2":
        from pymoo.algorithms.moo.nsga2 import NSGA2
        from pymoo.core.problem import Problem
        from pymoo.optimize import minimize
        rows = []
        class Synthetic(Problem):
            def __init__(self):
                super().__init__(n_var=dimension, n_obj=2, n_ieq_constr=1, xl=0.0, xu=1.0)
            def _evaluate(self, X, out, *args, **kwargs):
                observations = [row(values) for values in X]
                rows.extend(observations)
                out["F"] = np.asarray([[point["metrics"]["f1"], point["metrics"]["f2"]] for point in observations])
                out["G"] = np.asarray([[point["metrics"]["constraint"]] for point in observations])
        minimize(Synthetic(), NSGA2(pop_size=len(initial), sampling=initial.copy()),
                 termination=("n_eval", budget), seed=seed, verbose=False)
    elif method == "mobo":
        rows = [row(values) for values in initial]
        # All temporary comparison inputs are outside the repository and removed
        # on exit. Only the final evidence under --output is durable.
        with tempfile.TemporaryDirectory(prefix="ado-mobo-benchmark-") as directory:
            root = Path(directory)
            (root / "template.scs").write_text("// synthetic benchmark only")
            campaign = root / "campaigns" / "benchmark"
            campaign.mkdir(parents=True)
            config = {"template": "template.scs", "parameters": {
                f"x{i}": {"type": "number", "initial": ".5", "min": 0, "max": 1} for i in range(dimension)},
                "metrics": {name: {"kind": "scalar", "key": name} for name in ("f1", "f2", "constraint")},
                "rules": {"constraint": {"max": 0}},
                "campaign": {"max_total_points": budget, "max_points_per_cycle": len(initial),
                             "max_parallel": 4, "max_cycles": budget,
                             "metric_directions": {"f1": "min", "f2": "min"},
                             "bo": {"seed": seed, "batch_size": 4, "ref_point": REFERENCE,
                                    "fit_maxiter": 40, "acq_maxiter": 30, "num_restarts": 2,
                                    "raw_samples": 32, "mc_samples": 32, **(bo_settings or {})}}}
            path = root / "project.json"
            write_json(path, config)
            write_json(campaign / "cycle-0001_comparison.json", {"points": rows.copy()})
            cycle = 2
            while len(rows) < budget:
                proposal = propose({"project_config": str(path), "campaign_id": "benchmark"})
                # A benchmark harness explicitly evaluates reviewed proposals;
                # the proposer itself performs no evaluation or submission.
                batch = [row([float(point["parameters"][f"x{i}"]) for i in range(dimension)]) for point in proposal["points"]]
                rows.extend(batch)
                proposals.append({"mode": proposal["meta"]["mode"], "acquisition": proposal["meta"]["acquisition"],
                                  "hash": hashlib.sha256(json.dumps(proposal["points"], sort_keys=True).encode()).hexdigest()})
                write_json(campaign / f"cycle-{cycle:04d}_comparison.json", {"points": batch})
                cycle += 1
    else:
        raise ValueError(f"Unknown method: {method}")
    if len(rows) != budget:
        raise RuntimeError(f"{method} used {len(rows)} evaluations, expected {budget}")
    return {"method": method, "dimension": dimension, "seed": seed,
            "evaluations": budget, "initial_count": len(initial),
            "wall_seconds": time.perf_counter() - started,
            "hypervolume": hv_curve(rows), "observations": rows, "proposals": proposals}


def save_results(output, results, versions):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "results.json", {"reference_point_minimization": REFERENCE,
                                         "library_versions": versions, "results": results})
    with (output / "curves.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["dimension", "method", "seed", "evaluation", "hypervolume"])
        for result in results:
            writer.writerows((result["dimension"], result["method"], result["seed"], i, value)
                             for i, value in enumerate(result["hypervolume"], 1))
    dimensions = sorted({result["dimension"] for result in results})
    fig, axes = plt.subplots(1, len(dimensions), figsize=(6 * len(dimensions), 4), squeeze=False)
    summary = []
    for dimension, axis in zip(dimensions, axes[0]):
        for method in METHODS:
            selected = [result for result in results if result["dimension"] == dimension and result["method"] == method]
            curves = np.asarray([result["hypervolume"] for result in selected])
            mean, std = curves.mean(axis=0), curves.std(axis=0, ddof=1)
            x = np.arange(1, len(mean) + 1)
            axis.plot(x, mean, label=method)
            axis.fill_between(x, mean - std, mean + std, alpha=.15)
            summary.append({"dimension": dimension, "method": method,
                            "final_hv_mean": float(mean[-1]), "final_hv_std": float(std[-1]),
                            "mean_wall_seconds": float(np.mean([result["wall_seconds"] for result in selected]))})
        axis.set_title(f"Constrained ZDT1-like, d={dimension}")
        axis.set_xlabel("Number of evaluations")
        axis.set_ylabel("Feasible observed hypervolume (higher is better)")
        axis.grid(True, alpha=.3)
        axis.legend()
    fig.tight_layout()
    fig.savefig(output / "hypervolume.png", dpi=150)
    plt.close(fig)
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, default=48)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--dimensions", type=int, nargs="+", default=[2, 4])
    parser.add_argument("--output", type=Path, default=Path("docs/bench_mobo"))
    args = parser.parse_args()
    if args.seeds < 2 or any(dimension < 2 for dimension in args.dimensions):
        parser.error("At least two seeds and dimension >=2 are required")
    versions = {name: version(name) for name in ("botorch", "gpytorch", "torch", "scipy", "pymoo", "numpy", "matplotlib")}
    results = []
    for dimension in args.dimensions:
        for seed in range(args.seeds):
            for method in METHODS:
                result = benchmark(method, dimension, seed, args.budget)
                results.append(result)
                print(f"d={dimension} seed={seed} method={method}: n={result['evaluations']} HV={result['hypervolume'][-1]:.6g}", flush=True)
    save_results(args.output, results, versions)


if __name__ == "__main__":
    main()
