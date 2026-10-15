import copy
import io
import sys
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import sha256
from analog_agent.layout_constraints import topology
from analog_agent.layout_floorplan import validate_floorplan
from analog_agent.layout_floorplan_plugin import floorplan_generate
from analog_agent.layout_floorplan_score import score_floorplan, floorplan_score
from analog_agent.layout_runs import layout_context
from test_layout_floorplan_plugin import FloorplanPluginFixture
from test_layout_floorplan_rules import rules


class FloorplanScoreTest(FloorplanPluginFixture):
    def sample(self):
        cfg, _ = layout_context(self.run)
        floorplan = validate_floorplan(self.floorplan, cfg)
        i0 = floorplan["instances"][0]
        i0["bbox"] = [[0, 0], [10, 10]]
        i1 = copy.deepcopy(i0)
        i1.update(instance="I1", x=20, bbox=[[20, 0], [30, 10]])
        floorplan["instances"].append(i1)
        graph = topology("I0 (A B) resistor r=1k\nI1 (A C) resistor r=1k\n")
        return floorplan, graph

    def test_known_area_aspect_hpwl_and_weights(self):
        floorplan, graph = self.sample()
        preferences = rules()
        preferences["objective_weights"] = {"area": 1, "wirelength": 2, "aspect_ratio": 3}
        result = score_floorplan(floorplan, {"constraints": []}, graph, preferences)
        self.assertEqual(result["area_nm2"], 300)
        self.assertEqual(result["aspect_ratio"], 3)
        self.assertEqual(result["hpwl"]["estimate_nm"], 20)
        self.assertEqual(result["weighted_cost"], 346)
        self.assertTrue(result["hpwl"]["complete"])
        self.assertEqual(result, score_floorplan(floorplan, {"constraints": []}, graph, preferences))

    def test_manual_plugin_same_ruler_and_translation_invariance(self):
        floorplan, graph = self.sample()
        plugin = {**floorplan, "source": "plugin", "plugin_sha256": "1" * 64}
        first = score_floorplan(floorplan, {"constraints": []}, graph, rules())
        self.assertEqual(first, score_floorplan(plugin, {"constraints": []}, graph, rules()))
        moved = copy.deepcopy(floorplan)
        for item in moved["instances"]:
            item["x"] += 100
            item["bbox"] = [[x + 100, y] for x, y in item["bbox"]]
        result = score_floorplan(moved, {"constraints": []}, graph, rules())
        self.assertEqual(result["hpwl"], first["hpwl"])
        self.assertEqual(result["area_nm2"], first["area_nm2"])

    def test_compliance_and_target_bounds(self):
        floorplan, graph = self.sample()
        symmetry = {"id": "s", "kind": "symmetry", "source": "user", "rationale": "axis", "pairs": [["I0", "I1"]], "axis": {"direction": "x", "coordinate_nm": 15}, "tolerance_nm": 0}
        matching = {"id": "m", "kind": "matching", "source": "user", "rationale": "pattern", "groups": [["I0"], ["I1"]], "style": "common-centroid", "tolerance_nm": 0}
        preferences = rules()
        preferences["targets"] = {"area_nm2": {"min": 100, "max": 300}, "aspect_ratio": {"min": 1, "max": 2}}
        result = score_floorplan(floorplan, {"constraints": [symmetry, matching]}, graph, preferences)
        self.assertEqual(result["compliance"]["symmetry"]["fraction"], 1)
        self.assertEqual(result["compliance"]["matching"]["fraction"], 0)
        self.assertEqual(result["components"]["area"], 1)
        self.assertEqual(result["target_violations"], ["aspect_ratio"])

    def test_incomplete_and_power_never_claim_zero_cost(self):
        floorplan, graph = self.sample()
        preferences = {**rules(), "objective_weights": {"wirelength": 1}}
        graph["warnings"] = ["unknown model"]
        result = score_floorplan(floorplan, {"constraints": []}, graph, preferences)
        self.assertFalse(result["hpwl"]["complete"])
        self.assertIsNone(result["weighted_cost"])
        graph["warnings"] = []
        floorplan["instances"].pop()
        result = score_floorplan(floorplan, {"constraints": []}, graph, preferences)
        self.assertEqual(result["hpwl"]["skipped_nets"], ["A", "C"])
        self.assertIsNone(result["weighted_cost"])
        preferences["objective_weights"] = {"power": 1}
        self.assertIsNone(score_floorplan(floorplan, {"constraints": []}, graph, preferences)["weighted_cost"])

    def test_command_is_read_only_and_pins_input_hashes(self):
        self.configure()
        floorplan_generate(self.run, placement=self.placement)
        before = sha256(self.run / "task.json")
        self.mock_reader.reset_mock()
        candidate = floorplan_score(self.run)
        self.mock_reader.assert_not_called()
        live = floorplan_score(self.run, source="live")
        self.assertEqual(candidate["placement_sha256"], live["placement_sha256"])
        self.assertIn("netlist_sha256", candidate)
        self.assertEqual(sha256(self.run / "task.json"), before)

    def test_cli_score_source(self):
        with patch.object(sys, "argv", ["analog-agent", "layout", "floorplan-score", str(self.run), "--source", "live"]), patch("analog_agent.cli.floorplan_score", return_value={}) as score, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(), 0)
        score.assert_called_once_with(self.run, source="live")
