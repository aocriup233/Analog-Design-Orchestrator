import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analog_agent.core import sha256, write_json
from analog_agent.layout_config import CONFIG_PATH, load_layout_config, verification_inputs


class LayoutConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.rules = self.base / "rules"
        self.rules.mkdir()
        self.work = self.base / "work"
        self.work.mkdir()
        self.cfg = {"tool": "pegasus", "library": "user_lib", "cell": "user_cell",
                    "drc": {"rule_dir": str(self.rules), "run_dir": str(self.work)},
                    "lvs": {"rule_dir": str(self.rules), "run_dir": str(self.work)}}

    def save(self):
        write_json(self.root / CONFIG_PATH, {"layout": self.cfg})

    def test_private_bytes_hashed_and_optional_runsets(self):
        self.save()
        cfg, hashes = load_layout_config(self.root)
        self.assertEqual(cfg, self.cfg)
        self.assertEqual(hashes, {CONFIG_PATH: sha256(self.root / CONFIG_PATH)})
        runset = self.rules / "verification.runset"
        runset.write_bytes(b"fixture, not a foundry rule")
        self.cfg["drc"]["runset"] = str(runset)
        self.save()
        cfg, _ = load_layout_config(self.root)
        report = verification_inputs(self.root, cfg, "drc", [str(runset)])
        self.assertEqual(report["rule_sha256"], {str(runset.resolve()): sha256(runset)})
        with self.assertRaises(ValueError):
            verification_inputs(self.root, cfg, "drc", [])

    def test_reject_invalid_config_and_paths(self):
        for field, invalid in (("tool", "shell"), ("library", 'a\" system()'),
                               ("cell", "../escape"), ("timeout_s", 0),
                               ("timeout_s", float("inf")), ("skill", "system()")):
            with self.subTest(field=field):
                original = dict(self.cfg)
                self.cfg[field] = invalid
                # Infinity cannot be written as JSON; test a string instead.
                if invalid == float("inf"):
                    self.cfg[field] = "inf"
                self.save()
                with self.assertRaises(ValueError):
                    load_layout_config(self.root)
                self.cfg = original
        for value in ("rules", str(self.root), str(self.base / "missing")):
            self.cfg["drc"]["rule_dir"] = value
            self.save()
            with self.assertRaises((ValueError, FileNotFoundError)):
                load_layout_config(self.root)

    def test_readability_and_used_file_confinement(self):
        self.save()
        with patch("analog_agent.layout_config.os.access", return_value=False):
            with self.assertRaises(ValueError):
                load_layout_config(self.root)
        cfg, _ = load_layout_config(self.root)
        other = self.work / "unrelated.txt"
        other.write_bytes(b"not a rule")
        with self.assertRaises(ValueError):
            verification_inputs(self.root, cfg, "lvs", [str(other)])

    def test_reader_hash_and_path_validation(self):
        reader = self.root / "private/layout/reader.py"
        reader.parent.mkdir(parents=True)
        reader.write_text("def read(context): return {}\n", encoding="utf-8")
        self.cfg["bridge"] = {"reader": "private/layout/reader.py:read"}
        self.save()
        _, hashes = load_layout_config(self.root)
        self.assertEqual(hashes["private/layout/reader.py"], sha256(reader))
        self.cfg["bridge"]["reader"] = "../other.py:read"
        self.save()
        with self.assertRaises(ValueError):
            load_layout_config(self.root)
