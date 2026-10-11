import json
from unittest.mock import patch

from analog_agent.core import read_json, write_json
from analog_agent.layout_constraints import constraints_draft, topology, detect_constraints
from test_layout_constraints import LayoutConstraintsFixture, matching


class DraftReviewTest(LayoutConstraintsFixture):
    def test_memory_failure_reason_and_override_diff(self):
        entry = {"id": "broken", "kind": "layout_constraints", "summary": '{"constraints":['}
        with patch("analog_agent.layout_constraints.memory.retrieve", return_value={"entries": [entry]}):
            result = constraints_draft(self.run)
        draft = read_json(self.run / result["path"])
        self.assertIn("broken not adopted: invalid JSON", draft["warnings"][0])
        self.assertEqual(draft["coverage"]["mapped_devices"], 2)
        override = dict(draft["constraints"][0], groups=[["I0"], ["I1"]], style="common-centroid")
        write_json(self.root / "private/layout/constraints.json", {"schema_version": 1, "constraints": [override]})
        constraints_draft(self.run)
        diff = read_json(self.run / result["path"])["overrides"][0]
        self.assertEqual(diff["before"]["groups"], [["I0", "I1"]])
        self.assertEqual(diff["after"]["groups"], [["I0"], ["I1"]])
        self.assertIn("groups", [c["field"] for c in diff["changes"]])

    def test_orphan_detection_id_and_instance_fail_without_state_change(self):
        from analog_agent.state import status
        for item in (dict(matching(), id="det_gone"), dict(matching(), groups=[["I0", "renamed"]])):
            write_json(self.root / "private/layout/constraints.json", {"schema_version": 1, "constraints": [item]})
            with self.assertRaisesRegex(ValueError, "Orphan"):
                constraints_draft(self.run)
            self.assertEqual(status(self.run), "LAYOUT_PREPARED")

    def test_coverage_counts_unmapped_and_skipped_hierarchy(self):
        graph = topology("subckt child ()\nR0 (A B) resistor r=1k\nends child\nX0 () child m=2\nU0 (A B) unknown\nR1 (A B) resistor r=1k")
        self.assertEqual(graph["coverage"]["skipped_hierarchy"], 1)
        self.assertEqual(graph["coverage"]["unmapped_devices"], 1)
        self.assertEqual(graph["coverage"]["mapped_devices"], 1)

    def test_warning_freeze_requires_explicit_ack_and_pins_warning_set(self):
        from analog_agent.layout_constraints import constraints_freeze, digest
        from analog_agent.state import status
        snapshot = {"entries": [{"id": "broken", "kind": "layout_constraints", "summary": "{"}]}
        with patch("analog_agent.layout_constraints.memory.retrieve", return_value=snapshot):
            constraints_draft(self.run)
            with self.assertRaisesRegex(ValueError, "ack-warnings"):
                constraints_freeze(self.run, "reason alone is insufficient")
            self.assertEqual(status(self.run), "CONSTRAINTS_DRAFTED")
            ref = constraints_freeze(self.run, "reviewed coverage and rejected memory", ack_warnings=True)
        receipt = read_json(self.run / ref["path"])
        self.assertTrue(receipt["ack_warnings"])
        self.assertEqual(receipt["warnings_sha256"], digest(receipt["document"]["warnings"]))
        self.assertEqual(receipt["coverage_sha256"], digest(receipt["document"]["coverage"]))
