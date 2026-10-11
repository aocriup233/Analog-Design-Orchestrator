"""Documentation contract only; no production behavior or simulator calls."""
import json
import re
import unittest
from pathlib import Path


class SimulationCostDocumentTest(unittest.TestCase):
    def setUp(self):
        self.document = (Path(__file__).resolve().parents[1] /
                         "docs/design/simulation-cost.md").read_text(encoding="utf-8")

    def test_each_strategy_has_required_design_sections(self):
        for letter in "ABCDE":
            section = re.search(rf"^## {letter}\. .*?(?=^## |\Z)",
                                self.document, re.M | re.S)
            self.assertIsNotNone(section, letter)
            for heading in ("Data model and state machine", "Failure modes and provenance",
                            "Tests and expected savings"):
                self.assertIn("### " + heading, section[0])

    def test_example_is_unexecuted_and_boundaries_are_explicit(self):
        examples = re.findall(r"```json\n(.*?)\n```", self.document, re.S)
        self.assertEqual(len(examples), 1)
        example = json.loads(examples[0])
        self.assertEqual(example["schema_version"], 1)
        self.assertEqual(example["outcome"], "NOT_EVALUATED")
        self.assertIsNone(example["run_id"])
        self.assertIsNone(example["review_decision"])
        for contract in ("proposal only; not implemented", "Proposals are never auto-submitted",
                         "SHA-256", "never mixed into high-fidelity Pareto sets",
                         "not-yet-analyzed", "reconcile", "Recommended order", "Open questions"):
            self.assertIn(contract, self.document)

    def test_cost_examples_are_assumptions_not_measurements(self):
        for expression in ("1 + 0.5*4 + 0.5*0.4*20 = 7", "3 + 0.2*(10-3) = 4.4",
                           "1+0.3*20=7", "0.75*20+0.1=15.1", "80*1+20*20=480"):
            self.assertIn(expression, self.document)
        self.assertIn("not additive and not measured", self.document)
        self.assertIn("including proposer overhead and cases where it loses", self.document)
