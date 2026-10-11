import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent.sim_rules import render_rule


class PythonRuleTest(unittest.TestCase):
    def test_single_line_only(self):
        rule = {"kind": "python", "function": "rule.py:render"}
        with tempfile.TemporaryDirectory() as directory:
            for text in ('include "x"', 'ahdl_include "x"', "simulator lang=spectre",
                         "ac ac start=1\ninclude x", "ac ac start=1\\", "ac ac; dc dc", "", "\rac ac"):
                with patch("analog_agent.sim_rules.load_function", return_value=lambda _: text):
                    with self.subTest(text=text), self.assertRaisesRegex(ValueError, "single-line"):
                        render_rule(rule, Path(directory))
            with patch("analog_agent.sim_rules.load_function", return_value=lambda _: "noise noise start=1 stop=1G"):
                self.assertEqual(render_rule(rule, Path(directory)), "noise noise start=1 stop=1G")
