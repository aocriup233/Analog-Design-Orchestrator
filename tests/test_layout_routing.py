import copy
import unittest

from analog_agent.layout_routing import validate_plan, validate_tech


class RoutingSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tech = {"layers": {"metal": {"min_width_nm": 10, "min_spacing_nm": 5}}, "vias": {}}
        self.context = {"inputs": {"hash": "fixture"}, "tech_summary": self.tech,
                        "allowed_nets": ["A"], "nets": ["A", "VSS"], "violation_ids": [],
                        "constraints": {"constraints": []}}
        self.plan = {"schema_version": 1, "inputs": self.context["inputs"], "net_order": ["A"],
                     "symmetric_pairs": [], "violation_ids": [], "routes": [{"id": "routeA", "net": "A",
                     "layer": "metal", "width_nm": 10, "spacing_nm": 5, "shielding": None,
                     "guide_points": [[0, 0], [100, 0]], "vias": []}]}

    def test_deterministic_schema_and_reject_execution_fields(self):
        original = copy.deepcopy(self.plan)
        self.assertEqual(validate_plan(self.plan, self.context, {"grid_nm": 1}), self.plan)
        self.assertEqual(original, self.plan)
        for name in ("skill", "shell", "callback", "move_instance"):
            with self.assertRaises(ValueError):
                validate_plan({**self.plan, name: "malicious"}, self.context, {"grid_nm": 1})

    def test_private_tech_and_grid_and_limits(self):
        with self.assertRaises(ValueError):
            validate_tech({**self.tech, "rule_content": "forbidden"})
        for field, value in (("width_nm", 9), ("spacing_nm", 4), ("guide_points", [[0, 0], [0.1, 0]]),
                             ("guide_points", [[0, 0], [10, 20]]), ("layer", "system()"), ("net", "unknown")):
            bad = copy.deepcopy(self.plan)
            bad["routes"][0][field] = value
            with self.assertRaises(ValueError):
                validate_plan(bad, self.context, {"grid_nm": 1})

    def test_constraints_and_fix_scope(self):
        self.context["constraints"]["constraints"] = [{"kind": "keep_out", "bbox_nm": [[20, -20], [30, 20]]}]
        with self.assertRaisesRegex(ValueError, "keep-out"):
            validate_plan(self.plan, self.context, {"grid_nm": 1})
        self.context["constraints"]["constraints"] = []
        self.context["violation_ids"] = ["v1"]
        with self.assertRaisesRegex(ValueError, "violation"):
            validate_plan(self.plan, self.context, {"grid_nm": 1})

    def test_via_count_and_symmetric_pairs(self):
        self.tech["layers"]["upper"] = {"min_width_nm": 10, "min_spacing_nm": 5}
        self.tech["vias"]["join"] = {"layers": ["metal", "upper"], "min_enclosure_nm": 1}
        self.plan["routes"][0]["vias"] = [{"via": "join", "xy": [0, 0], "orientation": "R0"}]
        self.context["constraints"]["constraints"] = [{"kind": "max_vias", "nets": ["A"], "max_count": 0}]
        with self.assertRaisesRegex(ValueError, "via count"):
            validate_plan(self.plan, self.context, {"grid_nm": 1})
