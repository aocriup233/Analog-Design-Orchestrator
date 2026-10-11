import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from analog_agent.plugins import load_function


class PluginSecurityTest(unittest.TestCase):
    def test_allowlist_and_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module in ("builtins", "os", "subprocess", "shutil", "importlib", "analog_agent.proposers_evil"):
                with self.assertRaises(ValueError):
                    load_function(module + ":run", root)
            function = lambda: 1
            with patch("analog_agent.plugins.importlib.import_module", return_value=SimpleNamespace(run=function)) as imp:
                self.assertIs(load_function("analog_agent.proposers.mobo:run", root), function)
                imp.assert_called_once()
                self.assertIs(load_function("trusted:run", root, {"plugins": {"allowed_modules": ["trusted"]}}), function)

    def test_local_aliases_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "plugin.py"
            path.write_text("def run(): return 1\n")
            self.assertEqual(load_function("plugin.py:run", root)(), 1)
            with self.assertRaises(ValueError):
                load_function("../plugin.py:run", root)
            for code in ("from os import system as run", "from builtins import eval as run",
                         "from subprocess import run", "from shutil import rmtree as run",
                         "from importlib import import_module as run",
                         "from functools import partial\nfrom os import system\nrun = partial(system, 'echo bad')"):
                path.write_text(code)
                with self.subTest(code=code), self.assertRaisesRegex(ValueError, "Forbidden"):
                    load_function("plugin.py:run", root)
