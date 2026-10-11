import json
import re
import tempfile
import unittest
from pathlib import Path

from analog_agent.core import load_config, write_json
from analog_agent.proposers.mobo import Space, _reference
from analog_agent.campaign import _policy


class MoboDocumentationTest(unittest.TestCase):
    def test_documented_configuration_matches_contract(self):
        repository = Path(__file__).resolve().parents[1]
        document = (repository / "docs/mobo.md").read_text(encoding="utf-8")
        config = json.loads(re.search(r"```json\n(.*?)\n```", document, re.S)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "design.scs").write_text("// documentation fixture")
            write_json(root / "project.json", config)
            loaded = load_config(root / "project.json")
            self.assertEqual(_policy(loaded)["max_parallel"], 4)
            space = Space(loaded["parameters"])
            seed = loaded["campaign"]["bo"]["seed_points"][0]
            self.assertEqual(space.values(seed), seed)
            space.encode(seed)
            self.assertEqual(_reference(["objective_a", "objective_b"], loaded["campaign"]["metric_directions"],
                                        loaded["rules"], loaded["campaign"]["bo"]), [-10, -10])
        self.assertIn("never creates runs", document)
        self.assertIn("untrained empirical priors", document)
