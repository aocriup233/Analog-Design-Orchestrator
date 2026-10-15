import unittest
from unittest.mock import patch

from analog_agent.core import read_json, sha256
from analog_agent.layout_floorplan_consistency import check_consistency
from analog_agent.layout_floorplan_plugin import floorplan_generate
from analog_agent.state import status
from test_layout_floorplan_rules import rules
from test_layout_floorplan_plugin import FloorplanPluginFixture


class ConsistencyTest(unittest.TestCase):
    def document(self):
        return {"constraints": [
            {"id": "sym", "kind": "symmetry", "axis": {"direction": "x", "coordinate_nm": 1000}},
            {"id": "match", "kind": "matching", "style": "common-centroid"}]}

    def test_consistent_numeric_atoms(self):
        data = rules()
        data["symmetry_axes"] = [{"id": "sym", "direction": "x", "coordinate_nm": "1k"}]
        data["matching_patterns"] = [{"group_id": "match", "style": "common-centroid"}]
        self.assertEqual(check_consistency(data, self.document())["status"], "CONSISTENT")

    def test_conflicts_are_all_listed(self):
        data = rules()
        data["symmetry_axes"] = [{"id": "sym", "direction": "y", "coordinate_nm": 1001}, {"id": "unknown", "direction": "x", "coordinate_nm": 0}]
        data["matching_patterns"] = [{"group_id": "match", "style": "none"}]
        with self.assertRaises(ValueError) as error:
            check_consistency(data, self.document())
        for label in ("sym", "unknown", "match", "common-centroid"):
            self.assertIn(label, str(error.exception))

    def test_same_direction_different_coordinate_and_wrong_kind(self):
        data = rules()
        data["symmetry_axes"] = [{"id": "sym", "direction": "x", "coordinate_nm": 0}]
        with self.assertRaisesRegex(ValueError, "conflicts"):
            check_consistency(data, self.document())
        data["symmetry_axes"][0]["id"] = "match"
        with self.assertRaisesRegex(ValueError, "same ID"):
            check_consistency(data, self.document())


class GenerationConsistencyTest(FloorplanPluginFixture):
    def test_conflict_before_any_generator_or_bridge_call(self):
        data = rules()
        data["matching_patterns"] = [{"group_id": "unknown_group", "style": "common-centroid"}]
        self.configure(data)
        before = sha256(self.run / "task.json")
        self.mock_reader.reset_mock()
        with patch("analog_agent.layout_floorplan_plugin.execute") as generate, self.assertRaisesRegex(ValueError, "unknown_group"):
            floorplan_generate(self.run, placement=self.placement)
        generate.assert_not_called()
        self.mock_reader.assert_not_called()
        self.assertEqual(sha256(self.run / "task.json"), before)
        self.assertEqual(status(self.run), "FLOORPLAN_PENDING")

    def test_consistency_recorded_in_generation_proof(self):
        self.configure()
        result = floorplan_generate(self.run, placement=self.placement)
        self.assertEqual(read_json(self.run / result["generation"]["path"])["consistency"]["status"], "CONSISTENT")
