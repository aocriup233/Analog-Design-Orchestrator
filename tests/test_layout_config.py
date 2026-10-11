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
        self.cfg = {"tool": "pegasus", "library": "user_lib", "cell": "user_cell", "grid_nm": 1,
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

    def test_overlap_equal_inside_containing_and_cross_stage(self):
        child = self.rules / "child"
        child.mkdir()
        for stage in ("drc", "lvs"):
            for work in (self.rules, child, self.base):
                with self.subTest(stage=stage, work=work):
                    self.cfg[stage]["run_dir"] = str(work)
                    self.save()
                    with self.assertRaisesRegex(ValueError, "overlaps"):
                        load_layout_config(self.root)
                self.cfg[stage]["run_dir"] = str(self.work)
        # DRC work only overlaps the LVS rules (and vice versa).
        for run_stage, rule_stage in (("drc", "lvs"), ("lvs", "drc")):
            old = self.cfg[rule_stage]["rule_dir"]
            self.cfg[rule_stage]["rule_dir"] = str(self.work)
            self.save()
            with self.assertRaisesRegex(ValueError, "overlaps"):
                load_layout_config(self.root)
            self.cfg[rule_stage]["rule_dir"] = old

    def test_writable_and_all_offending_names(self):
        import os
        self.save()
        with patch("analog_agent.layout_config.os.access", side_effect=lambda path, mode: mode != os.W_OK):
            with self.assertRaisesRegex(ValueError, "writable"):
                load_layout_config(self.root)
        self.cfg.update(library="1lib", cell="2cell")
        self.save()
        with self.assertRaises(ValueError) as raised:
            load_layout_config(self.root)
        self.assertIn("1lib", str(raised.exception))
        self.assertIn("2cell", str(raised.exception))
        self.cfg["allow_leading_digit"] = True
        self.save()
        load_layout_config(self.root)

    def test_large_files_streamed_in_one_mib_chunks(self):
        from analog_agent.layout_config import stream_sha256
        path = self.rules / "large.rule"
        path.write_bytes(b"fixture" * 400000)
        expected = sha256(path)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must stream")):
            self.assertEqual(stream_sha256(path), expected)
