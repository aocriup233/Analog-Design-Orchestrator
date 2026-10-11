import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from analog_agent.backends import BridgeBackend, LSFBackend, validate_spectre_cmd
from analog_agent.core import sha256


class BackendSecurityTest(unittest.TestCase):
    def test_command_and_includes(self):
        for command in ("spectre -64", "spectre;touch", "./spectre", "bad", "spectre\n", "$(evil)"):
            with self.assertRaises(ValueError):
                validate_spectre_cmd({"spectre_cmd": command})
        self.assertEqual(validate_spectre_cmd({}), "spectre")
        self.assertEqual(validate_spectre_cmd({"spectre_cmd": "/opt/bin/spectre"}), "/opt/bin/spectre")
        self.assertEqual(validate_spectre_cmd({"spectre_cmd": "custom", "allowed_spectre_commands": ["custom"]}), "custom")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "escapes"):
                BridgeBackend().stage(root, {"_project_root": str(root), "simulation": {"include_files": ["../outside"]}}, {"deck": str(root / "input.scs"), "deck_sha256": "x"})

    def test_retrieval_relative_and_remote_hash_required(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "raw").mkdir()
            file = run / "raw" / "result"
            file.write_text("result")
            digest = sha256(file)
            backend = object.__new__(LSFBackend)
            backend.adapter = Mock()
            artifact = {"path": "raw/result", "sha256": digest}
            backend.adapter.retrieve.return_value = {"artifacts": [artifact]}
            with self.assertRaisesRegex(ValueError, "remote_sha256"):
                backend.retrieve(run, {}, {})
            backend.retrieve(run, {"simulation": {"require_remote_sha256": False}}, {})
            self.assertEqual(artifact["path"], str(file))
            artifact["remote_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "mismatch"):
                backend.retrieve(run, {}, {})
            artifact["remote_sha256"] = digest
            backend.retrieve(run, {}, {})
            artifact["path"] = "../outside"
            with self.assertRaisesRegex(ValueError, "escapes"):
                backend.retrieve(run, {}, {})
