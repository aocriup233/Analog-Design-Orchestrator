"""The distribution gate must reject private data even inside a source tree."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('release_check',
    Path(__file__).resolve().parents[1] / 'scripts' / 'check_public_release.py')
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_private_and_process_payloads_rejected(self):
        for name in ('private/table.csv', 'runs/raw.csv', 'output/result.json',
                     'analog_agent/table.csv', 'analog_agent/model.scs',
                     'analog_agent/private/secret.py',
                     'analog_agent-1.0.dist-info/table.csv',
                     '../secret.py', 'analog_agent/../../secret.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release_check.check_member(name, wheel=True)
        for name in ('pkg/private/table.csv', 'pkg/src/analog_agent/model.scs',
                     'pkg/src/analog_agent/private/secret.py',
                     'pkg/src/analog_agent/private/table.csv'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release_check.check_member(name, wheel=False)

    def test_public_code_allowed(self):
        release_check.check_member('analog_agent/gmid.py', wheel=True)
        release_check.check_member('pkg/src/analog_agent/gmid.py', wheel=False)
        release_check.check_member('pkg/README.md', wheel=False)
