import copy
import unittest

from analog_agent.core import read_json, write_json
from analog_agent.layout_config import load_layout_config
from analog_agent.layout_floorplan_rules import validate_rules, RULES_PATH
from analog_agent.layout_runs import prepare_layout, layout_context
from test_layout_runs import LayoutRunsFixture


def rules():
    return {"schema_version": 1, "units": "nm", "rows": [], "regions": [],
            "symmetry_axes": [], "matching_patterns": [], "spacing": [], "guard_rings": [],
            "keep_outs": [], "pin_sides": [], "targets": {}}


class FloorplanRulesTest(unittest.TestCase):
    def test_all_fields_and_no_rewrite(self):
        data = rules()
        data.update(rows=[{"id": "row", "bbox_nm": [[0, 0], [100, 100]], "instances": ["I0"], "orientation": "R0"}],
                    regions=[{"id": "region", "bbox_nm": [[0, 0], [100, 100]], "instances": ["I0"]}],
                    symmetry_axes=[{"id": "axis", "direction": "x", "coordinate_nm": -10}],
                    matching_patterns=[{"group_id": "pair", "style": "common-centroid"}],
                    spacing=[{"id": "spacing", "members": ["I0", "I1"], "min_nm": "10"}],
                    guard_rings=[{"id": "ring", "members": ["I0"], "width_nm": 5, "spacing_nm": 10, "layers": ["metal"]}],
                    keep_outs=[{"id": "ko", "bbox_nm": [[-10, -10], [0, 0]]}],
                    pin_sides=[{"pin": "IN", "side": "left"}],
                    targets={"aspect_ratio": {"min": 0.5, "max": 2}, "area_nm2": {"min": 1, "max": 10000}},
                    objective_weights={"area": 1})
        before = copy.deepcopy(data)
        self.assertIs(validate_rules(data), data)
        self.assertEqual(data, before)

    def test_invalid_and_injection(self):
        for value in (True, -1, "10 include", "1\\\ninclude", "1 // x", float("nan")):
            data = rules()
            data["spacing"] = [{"id": "s", "members": ["I0"], "min_nm": value}]
            with self.assertRaises(ValueError):
                validate_rules(data)
        for key, value in (("units", "um"), ("rows", {}), ("objective_weights", {"raw_skill": 1}), ("targets", {"aspect_ratio": {"min": 2, "max": 1}})):
            with self.assertRaises(ValueError):
                validate_rules({**rules(), key: value})


class FloorplanConfigTest(LayoutRunsFixture):
    def test_manual_default_and_plugin_manifest(self):
        self.verify_source()
        self.assertNotIn(RULES_PATH, load_layout_config(self.root)[1])
        cfg = read_json(self.layout_config)
        cfg["layout"]["floorplan"] = {"mode": "plugin", "generator": "private/layout/generator.py:generate"}
        write_json(self.layout_config, cfg)
        with self.assertRaises(OSError):
            load_layout_config(self.root)
        write_json(self.root / RULES_PATH, rules())
        plugin = self.root / "private/layout/generator.py"
        plugin.write_text("def generate(context): return {}\n", encoding="utf-8")
        run = prepare_layout(self.source)
        manifest = read_json(run / "layout_inputs.json")["private_sha256"]
        self.assertIn(RULES_PATH, manifest)
        self.assertIn("private/layout/generator.py", manifest)
        plugin.write_text("def generate(context): return None\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            layout_context(run)

    def test_invalid_modes_and_references(self):
        original = read_json(self.layout_config)
        for value in ({"mode": "auto"}, {"mode": "plugin"}, {"mode": "manual", "generator": "x.py:f"}, {"mode": "plugin", "generator": "os:system"}, {"mode": "plugin", "generator": "../outside.py:generate"}):
            cfg = copy.deepcopy(original)
            cfg["layout"]["floorplan"] = value
            write_json(self.layout_config, cfg)
            with self.assertRaises((ValueError, OSError)):
                load_layout_config(self.root)
