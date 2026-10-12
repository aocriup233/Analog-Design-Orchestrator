import copy
import io
import sys
from unittest.mock import patch

from analog_agent import cli
from analog_agent.core import read_json, sha256, write_json
from analog_agent.layout_constraints import constraints_draft, constraints_freeze
from analog_agent.layout_floorplan import floorplan_export, floorplan_freeze, require_unchanged_floorplan
from analog_agent.layout_placement_check import check_placement, floorplan_constraints_check
from analog_agent.layout_runs import layout_lock
from analog_agent.state import status
from test_layout_floorplan import LayoutFloorplanFixture


def document(items):
    return {"schema_version": 1, "constraints": items}


def symmetry():
    return {"id": "axis", "kind": "symmetry", "source": "user", "rationale": "balance",
            "pairs": [["I0", "I1"]], "axis": {"direction": "x", "coordinate_nm": 0}, "tolerance_nm": 0}


class LayoutPlacementCheckTest(LayoutFloorplanFixture):
    def geometry(self):
        # Already canonical nm; pure check fixtures need no Bridge.
        return {"instances": [{"instance": "I" + str(i), "bbox": [[x - 1, -1], [x + 1, 1]],
                 "master": {"library": "generic", "cell": "unit"}, "params_sha256": "a" * 64}
                 for i, x in enumerate((-3, 3, -1, 1))]}

    def test_axis_symmetry_tolerance_and_missing_instances(self):
        floorplan = self.geometry()
        constraint = symmetry()
        self.assertEqual(check_placement(floorplan, document([constraint])), [])
        floorplan["instances"][1]["bbox"] = [[3, -1], [5, 1]]
        self.assertEqual(len(check_placement(floorplan, document([constraint]))), 1)
        constraint["tolerance_nm"] = 1
        self.assertEqual(check_placement(floorplan, document([constraint])), [])
        constraint["pairs"] = [["I0", "missing"]]
        self.assertIn("absent", check_placement(floorplan, document([constraint]))[0]["message"])

    def test_common_centroid_and_interdigitation(self):
        floorplan = self.geometry()
        item = {"id": "match", "kind": "matching", "source": "user", "rationale": "units",
                "groups": [["I0", "I1"], ["I2", "I3"]], "style": "common-centroid",
                "topology": "user", "tolerance_nm": 0}
        self.assertEqual(check_placement(floorplan, document([item])), [])
        item["groups"] = [["I0", "I2"], ["I1", "I3"]]
        self.assertIn("common centroid", check_placement(floorplan, document([item]))[0]["message"])
        item["style"] = "interdigitated"
        item["groups"] = [["I0", "I3"], ["I2", "I1"]]
        self.assertEqual(check_placement(floorplan, document([item])), [])
        item["groups"] = [["I0", "I2"], ["I3", "I1"]]
        self.assertIn("alternate", check_placement(floorplan, document([item]))[0]["message"])

    def test_keep_out_interior_and_touching(self):
        floorplan = self.geometry()
        item = {"id": "space", "kind": "keep_out", "source": "user", "rationale": "reserved",
                "bbox_nm": [[-5, -1], [-4, 1]]}
        self.assertEqual(check_placement(floorplan, document([item])), [])
        item["bbox_nm"][1][0] = -3
        self.assertEqual(check_placement(floorplan, document([item]))[0]["instances"], ["I0"])

    def test_equal_centroid_alone_does_not_prove_pattern(self):
        floorplan = self.geometry()
        # A's centers -3,-1,4 have mean 0 but no centrally symmetric pattern.
        floorplan["instances"][1]["bbox"] = [[3, -1], [5, 1]]
        floorplan["instances"][3]["bbox"] = [[-1, -1], [1, 1]]
        item = {"id": "match", "kind": "matching", "source": "user", "rationale": "units",
                "groups": [["I0", "I1", "I2"], ["I3"]], "style": "common-centroid",
                "topology": "user", "tolerance_nm": 0}
        self.assertTrue(any("centrosymmetric" in w["message"] for w in check_placement(floorplan, document([item]))))

    def test_global_nets_and_body_mismatch_not_false_matching(self):
        from analog_agent.layout_constraints import detect_constraints, topology
        cfg = {"models": {"dev": {"kind": "mos", "terminals": ["d", "g", "s", "body"]}}}
        graph = topology("global VSS\nsubckt stage ()\nM0 (D P VSS B0) dev\nM1 (Q N VSS B1) dev\nends stage\nX0 () stage", cfg)
        self.assertEqual(graph["devices"][0]["terms"]["s"], "VSS")
        self.assertEqual(detect_constraints(graph), [])

    def test_freeze_auto_checks_warning_without_mutation_and_hashes(self):
        item = {"id": "space", "kind": "keep_out", "source": "user", "rationale": "reserved",
                "bbox_nm": [[0, 0], [1000, 2000]]}
        write_json(self.root / "private/layout/constraints.json", document([item]))
        before = copy.deepcopy(self.floorplan)
        self.pending()
        floorplan_export(self.run)
        result = floorplan_freeze(self.run, "reviewed despite advisory warning")
        self.assertEqual(self.floorplan, before)
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")
        self.assertEqual(len(result["constraint_warnings"]), 1)
        receipt = read_json(self.run / result["path"])
        report = read_json(self.run / "floorplan_check.json")
        self.assertEqual(receipt["constraints_freeze"], report["constraints_freeze"])
        self.assertEqual(sha256(self.run / receipt["placement_check"]["path"]), receipt["placement_check"]["sha256"])
        with layout_lock(self.run) as held:
            floorplan_constraints_check(self.run, session=held)
            from analog_agent.layout_placement_check import routing_review
            routing_review(self.run, "proceed-with-warnings", "accept current geometric warnings", session=held)
            require_unchanged_floorplan(self.run, "routing", session=held)
        with patch.object(sys, "argv", ["analog-agent", "layout", "constraints-check", str(self.run)]), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(), 0)
            self.assertIn("WARNINGS", output.getvalue())

    def test_constraint_source_edit_blocks_checkpoint_before_bridge(self):
        self.freeze()
        self.mock_reader.reset_mock()
        write_json(self.root / "private/layout/constraints.json", document([]))
        with self.assertRaises(ValueError):
            require_unchanged_floorplan(self.run, "routing")
        self.mock_reader.assert_not_called()
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")

    def test_tampered_constraint_receipt_and_unfrozen_gate(self):
        with self.assertRaises(ValueError):
            floorplan_constraints_check(self.run)
        self.freeze()
        ref = read_json(self.run / "task.json")["active_constraints_freeze"]
        write_json(self.run / ref["path"], {})
        with self.assertRaises(ValueError):
            floorplan_constraints_check(self.run)
        self.assertEqual(status(self.run), "FLOORPLAN_FROZEN")
