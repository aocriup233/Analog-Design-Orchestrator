"""Synthetic comparisons only: no simulator, private files or run artifacts."""
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analog_agent.campaign import _validate_points, propose as campaign_propose, create_campaign
from analog_agent.core import write_json, read_json
from analog_agent.proposers.mobo import Space, hypervolume, propose


BO_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("torch", "gpytorch", "botorch", "scipy"))
SCIPY_AVAILABLE = importlib.util.find_spec("scipy") is not None


def synthetic(parameters):
    x, y = float(parameters["x"]), float(parameters["y"])
    scale = 1 + 9 * y
    return {"f1": x, "f2": scale * (1 - math.sqrt(x / scale)), "constraint": x + 0.25*y - 0.85}


class MoboTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "template.scs").write_text("// no simulator")
        self.cfg = {"template": "template.scs", "parameters": {
            "x": {"type": "number", "initial": "0.2", "min": 0, "max": 1},
            "y": {"type": "number", "initial": "0.1", "min": 0, "max": 1}},
            "metrics": {name: {"kind": "scalar", "key": name} for name in ("f1", "f2", "constraint")},
            "rules": {"constraint": {"max": 0}},
            "campaign": {"max_parallel": 2, "max_points_per_cycle": 3, "max_total_points": 30,
                         "metric_directions": {"f1": "min", "f2": "min"},
                         "proposer": "analog_agent.proposers.mobo:propose",
                         "bo": {"seed": 7, "ref_point": [1.1, 11], "fit_maxiter": 20,
                                "acq_maxiter": 20, "mc_samples": 16, "num_restarts": 2,
                                "raw_samples": 16, "classifier_steps": 20}}}
        self.path = self.root / "project.json"
        self.save()
        self.campaign = self.root / "campaigns" / "test"
        self.campaign.mkdir(parents=True)
        self.context = {"project_config": str(self.path), "campaign_id": "test"}

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        write_json(self.path, self.cfg)

    def comparison(self, points, cycle=1):
        rows = []
        for x, y in points:
            values = {"x": str(x), "y": str(y)}
            metrics = synthetic(values)
            rows.append({"parameters": values, "metrics": metrics,
                         "verdict": "PASS" if metrics["constraint"] <= 0 else "FAIL"})
        write_json(self.campaign / f"cycle-{cycle:04d}_comparison.json", {"points": rows})
        return rows

    def validate(self, result, prior=0):
        manifest = {"cycles": [{"points": [{}] * prior}] if prior else [],
                    "policy": {"max_points_per_cycle": 3, "max_total_points": 30, "max_cycles": 20}}
        points = _validate_points(manifest, result, self.cfg)
        for point in points:
            self.assertRegex(point["id"], r"^[A-Za-z0-9-]+$")
            self.assertIn("± std", point["rationale"])
            self.assertIn("feasibility probability", point["rationale"])
            self.assertIn("acquisition value", point["rationale"])

    def test_import_without_optional_stack(self):
        code = '''
import sys, importlib.abc
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch','gpytorch','botorch','scipy'}:
            raise ImportError('optional BO stack deliberately unavailable')
sys.meta_path.insert(0, Deny())
import analog_agent.cli
import analog_agent.proposers.mobo
assert not any(name in sys.modules for name in ('torch','gpytorch','botorch','scipy'))
'''
        completed = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_space_log_enum_and_unbounded_errors(self):
        space = Space({"R": {"type": "number", "min": "1m", "max": "1M", "log": True},
                       "mode": {"type": "enum", "candidates": ["fast", "slow"]}})
        self.assertAlmostEqual(space.encode({"R": "1", "mode": "slow"})[0], 1/3)
        self.assertEqual(space.decode([0, 1, 0]), {"R": "0.001", "mode": "fast"})
        self.assertEqual(space.key({"R": "1k", "mode": "fast"}), space.key({"R": "1000", "mode": "fast"}))
        self.assertEqual(len(space.fixed_features(2)), 2)
        with self.assertRaisesRegex(ValueError, "combinations"):
            space.fixed_features(1)
        with self.assertRaisesRegex(ValueError, "requires finite min and max"):
            Space({"R": {"type": "number", "initial": "1"}})

    def test_hypervolume(self):
        self.assertEqual(hypervolume([[2, 1], [1, 2], [0, 20]], [0, 0]), 3)
        self.assertEqual(hypervolume([], [0, 0]), 0)

    @unittest.skipUnless(SCIPY_AVAILABLE, "optional scipy")
    def test_cold_start_seed_dedup_and_byte_determinism(self):
        self.comparison([(0.2, 0.1)])
        self.cfg["campaign"]["bo"]["seed_points"] = [
            {"x": "0.2", "y": "0.1"}, {"x": "0.3", "y": "0"}, {"x": "0.3", "y": "0"}]
        self.save()
        first, second = propose(self.context), propose(self.context)
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))
        self.assertEqual(first["points"][0]["parameters"], {"x": "0.3", "y": "0"})
        self.assertEqual(first["meta"]["mode"], "cold_start")
        self.assertEqual(len(first["points"]), 2)
        self.validate(first, 1)
        self.assertIn("untrained", first["points"][0]["rationale"])

    @unittest.skipUnless(SCIPY_AVAILABLE, "optional scipy")
    def test_all_cycles_failures_missing_metrics_and_plateau(self):
        for cycle in range(1, 5):
            rows = self.comparison([(0.2, 0.1)], cycle)
            if cycle == 1:
                rows.extend([{"parameters": {"x": ".9", "y": ".3"}, "state": "FAILED"},
                             {"parameters": {"x": ".7", "y": ".4"}, "verdict": "PASS", "metrics": {"f1": .7}}])
                write_json(self.campaign / f"cycle-{cycle:04d}_comparison.json", {"points": rows})
        self.cfg["campaign"]["bo"].update(n_init=50, patience=3)
        self.save()
        result = propose({**self.context, "latest_comparison": {"points": []}})
        self.assertEqual(result["meta"]["data_count"], 6)
        self.assertEqual(result["meta"]["valid_count"], 4)
        self.assertEqual(result["meta"]["hard_failure_count"], 2)
        self.assertEqual(result["meta"]["infeasible_count"], 2)
        self.assertTrue(result["meta"]["stop_suggestion"])
        self.assertEqual(len(result["meta"]["comparison_sha256"]), 4)
        self.validate(result, 6)

    @unittest.skipUnless(SCIPY_AVAILABLE, "optional scipy")
    def test_budget_and_campaign_hook_review_only(self):
        self.comparison([(0.1, 0.1), (0.2, 0.2)])
        self.context["policy"] = {"max_total_points": 3, "max_points_per_cycle": 2, "max_parallel": 2}
        result = propose(self.context)
        self.assertEqual(len(result["points"]), 1)
        with self.assertRaisesRegex(ValueError, "remaining"):
            propose({**self.context, "policy": {"max_total_points": 2}})
        actual = create_campaign(self.path)
        receipt = campaign_propose(actual)
        self.assertTrue(receipt["review_required"])
        saved = read_json(Path(receipt["proposal"]))
        self.assertEqual(len(saved["points"]), 2)
        repeated = campaign_propose(actual)
        self.assertEqual(Path(receipt["proposal"]).read_bytes(), Path(repeated["proposal"]).read_bytes())
        self.assertEqual(read_json(actual / "campaign.json")["cycles"], [])
        self.assertFalse((self.root / "runs").exists())

    @unittest.skipUnless(SCIPY_AVAILABLE, "optional scipy")
    def test_tightened_bounds_exclude_history_not_budget(self):
        self.comparison([(.1, .1), (.9, .2)])
        self.cfg["parameters"]["x"]["max"] = .5
        self.save()
        result = propose(self.context)
        self.assertEqual(result["meta"]["data_count"], 1)
        self.assertEqual(result["meta"]["history_count"], 2)
        self.assertEqual(result["meta"]["excluded_history"][0]["fields"], ["x"])
        self.assertEqual(result["points"][0]["id"], "bo-000003")
        self.assertTrue(all(float(p["parameters"]["x"]) <= .5 for p in result["points"]))
        self.validate(result, 2)
        with self.assertRaisesRegex(ValueError, "remaining"):
            propose({**self.context, "policy": {"max_total_points": 2}})

    @unittest.skipUnless(SCIPY_AVAILABLE, "optional scipy")
    def test_hard_failure_warning_in_cold_start(self):
        rows = [{"parameters": {"x": str(x), "y": ".1"}, "state": "FAILED"}
                for x in (.1, .2, .3)]
        write_json(self.campaign / "cycle-0001_comparison.json", {"points": rows})
        result = propose(self.context)
        self.assertEqual(result["meta"]["mode"], "cold_start")
        self.assertEqual(result["meta"]["failure_rate"], 1)
        self.assertIn("netlist", result["meta"]["warnings"][0])
        self.cfg["campaign"]["bo"]["failure_warning_threshold"] = 1
        self.save()
        self.assertEqual(propose(self.context)["meta"]["warnings"], [])
        self.cfg["campaign"]["bo"]["failure_warning_threshold"] = 2
        self.save()
        with self.assertRaisesRegex(ValueError, "failure_warning_threshold"):
            propose(self.context)

    @unittest.skipUnless(BO_AVAILABLE, "optional BO stack")
    def test_warm_determinism_and_infeasible_classifier(self):
        rows = self.comparison([(0.1, .1), (.3, .15), (.5, .05), (.7, .2), (.9, .8)])
        rows.extend([{"parameters": {"x": ".8", "y": ".7"}, "state": "FAILED"},
                     {"parameters": {"x": ".75", "y": ".6"}, "verdict": "PASS", "metrics": {"f1": .75}}])
        write_json(self.campaign / "cycle-0001_comparison.json", {"points": rows})
        first, second = propose(self.context), propose(self.context)
        self.assertEqual(first, second)
        self.assertEqual(first["meta"]["mode"], "mobo")
        self.assertEqual(first["meta"]["classifier_observation_count"], 7)
        self.assertEqual(first["meta"]["gp_observation_counts"]["f1"], 6)
        self.assertIn(first["meta"]["acquisition"], ("qLogNEHVI", "qNEHVI"))
        self.validate(first, 7)

    @unittest.skipUnless(BO_AVAILABLE, "optional BO stack")
    def test_no_feasible_baseline(self):
        self.comparison([(.9, .1), (.95, .2), (.99, .3)])
        self.cfg["campaign"]["bo"].update(n_init=3, batch_size=1)
        self.save()
        result = propose(self.context)
        self.assertEqual(result["meta"]["current_hypervolume"], 0)
        self.assertEqual(result["meta"]["acquisition"], "probability_of_feasibility")
        self.assertLess(float(result["points"][0]["parameters"]["x"]), .9)
        self.assertEqual(result, propose(self.context))
        self.validate(result, 3)

    @unittest.skipUnless(BO_AVAILABLE, "optional BO stack")
    def test_warm_enum_output_is_legal(self):
        rows = self.comparison([(.1, .1), (.3, .15), (.5, .05), (.7, .2), (.9, .8)])
        self.cfg["parameters"]["mode"] = {"type": "enum", "initial": "fast", "candidates": ["fast", "slow"]}
        self.cfg["campaign"]["bo"].update(n_init=5, batch_size=1)
        self.save()
        for index, row in enumerate(rows):
            row["parameters"]["mode"] = "fast" if index % 2 else "slow"
        write_json(self.campaign / "cycle-0001_comparison.json", {"points": rows})
        result = propose(self.context)
        self.assertIn(result["points"][0]["parameters"]["mode"], ("fast", "slow"))
        self.validate(result, 5)
