import importlib.util
import json
import math
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("bench_mobo", Path(__file__).resolve().parents[1] / "scripts/bench_mobo.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchmarkTest(unittest.TestCase):
    def test_recorded_five_seed_results_and_summary(self):
        directory = Path(__file__).resolve().parents[1] / "docs/bench_mobo"
        results = json.loads((directory / "results.json").read_text())["results"]
        summary = json.loads((directory / "summary.json").read_text())
        self.assertEqual(len(results), 30)
        for dimension in (2, 4):
            for method in bench.METHODS:
                selected = [result for result in results if result["dimension"] == dimension and result["method"] == method]
                self.assertEqual({result["seed"] for result in selected}, set(range(5)))
                final = []
                for result in selected:
                    self.assertEqual(result["evaluations"], 48)
                    self.assertEqual(len(result["observations"]), 48)
                    self.assertEqual(bench.hv_curve(result["observations"]), result["hypervolume"])
                    self.assertTrue(all(left <= right + 1e-12 for left, right in zip(result["hypervolume"], result["hypervolume"][1:])))
                    final.append(result["hypervolume"][-1])
                mean = sum(final) / len(final)
                std = math.sqrt(sum((value-mean)**2 for value in final) / (len(final)-1))
                reported = next(item for item in summary if item["dimension"] == dimension and item["method"] == method)
                self.assertAlmostEqual(reported["final_hv_mean"], mean)
                self.assertAlmostEqual(reported["final_hv_std"], std)
            for seed in range(5):
                matched = [result for result in results if result["dimension"] == dimension and result["seed"] == seed]
                initial_count = matched[0]["initial_count"]
                self.assertTrue(all(item["observations"][:initial_count] == matched[0]["observations"][:initial_count] for item in matched))

    def test_problem_and_hypervolume_curve(self):
        self.assertEqual(bench.evaluate([0, 0]), {"f1": 0, "f2": 1, "constraint": -.85})
        self.assertEqual(bench.row([1, 1])["verdict"], "FAIL")
        curve = bench.hv_curve([bench.row([0, 0]), bench.row([1, 1]), bench.row([.5, 0])])
        self.assertEqual(curve[0], curve[1])
        self.assertGreater(curve[2], curve[1])

    @unittest.skipUnless(importlib.util.find_spec("scipy"), "optional scipy")
    def test_random_determinism_equal_budget_and_initial_design(self):
        first = bench.benchmark("random", 2, 0, 16)
        second = bench.benchmark("random", 2, 0, 16)
        self.assertEqual(first["observations"], second["observations"])
        self.assertEqual(len(first["hypervolume"]), 16)
        self.assertEqual(first["initial_count"], 8)
        with self.assertRaises(ValueError):
            bench.benchmark("random", 2, 0, 15)

    @unittest.skipUnless(importlib.util.find_spec("pymoo"), "optional benchmark pymoo")
    def test_nsga_budget_and_shared_initial_observations(self):
        random = bench.benchmark("random", 2, 0, 16)
        nsga = bench.benchmark("nsga2", 2, 0, 16)
        self.assertEqual(nsga["observations"][:8], random["observations"][:8])
        self.assertEqual(nsga["evaluations"], 16)
