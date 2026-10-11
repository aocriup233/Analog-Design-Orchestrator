import copy
import unittest
from unittest.mock import patch

from analog_agent.layout_constraints import digest
from analog_agent.layout_lvs_source import canonical_circuit
from analog_agent.layout_verification import verify
from analog_agent.state import status
from test_layout_verification import VerificationFixture


class CanonicalCircuitTest(unittest.TestCase):
    def setUp(self):
        self.document = {"schema_version": 1, "top": "top", "ports": ["A", "B"], "complete": True, "unparsed": [],
            "input_sha256": "fixture", "devices": [{"id": "R0", "model": "resistor", "terminals": {"p": "A", "n": "B"},
                                                       "parameters": {"r": {"type": "number", "value": "1k"}}}]}

    def test_scale_and_order_normalization_case_sensitive(self):
        other = copy.deepcopy(self.document)
        other["devices"][0]["parameters"]["r"]["value"] = "1000"
        self.assertEqual(digest(canonical_circuit(self.document)), digest(canonical_circuit(other)))
        other["devices"][0]["parameters"]["r"]["value"] = "1M"
        a = digest(canonical_circuit(other))
        other["devices"][0]["parameters"]["r"]["value"] = "1m"
        self.assertNotEqual(a, digest(canonical_circuit(other)))

    def test_connectivity_parameter_and_partial_parse_are_not_equivalent(self):
        original = digest(canonical_circuit(self.document))
        other = copy.deepcopy(self.document)
        other["devices"][0]["terminals"]["n"] = "C"
        self.assertNotEqual(original, digest(canonical_circuit(other)))
        for field, value in (("complete", False), ("unparsed", ["include unresolved"])):
            with self.assertRaises(ValueError):
                canonical_circuit({**self.document, field: value})


class LVSSourceProofTest(VerificationFixture):
    def test_current_design_difference_blocks_lvs_run(self):
        original = self.dispatch
        calls = []
        def changed(run, operation, payload):
            calls.append((operation, payload))
            result = original(run, operation, payload)
            if operation == "normalize_lvs_source" and payload["format"] == "cdl":
                result["ports"] = ["CHANGED"]
            return result
        with patch("analog_agent.layout_verification.execute", side_effect=changed), \
             patch("analog_agent.layout_lvs_source.execute", side_effect=changed), \
             patch("analog_agent.layout_worker.execute", side_effect=self.dispatch), self.assertRaises(ValueError):
            verify(self.run)
        self.assertFalse(any(op == "verify" and p["phase"] == "run" and p["stage"] == "lvs" for op, p in calls))
        self.assertEqual(status(self.run), "NEEDS_ATTENTION")
