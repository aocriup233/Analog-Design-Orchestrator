import os
import stat
import subprocess
import sys
from unittest.mock import patch

from analog_agent.core import read_json, write_json
from analog_agent.layout_bridge import ReaderInputError, read_placement
from analog_agent.layout_runs import prepare_layout
from test_layout_runs import LayoutRunsFixture


class LayoutBridgeSecurityTest(LayoutRunsFixture):
    def setUp(self):
        super().setUp()
        self.verify_source()

    def reader_run(self, source):
        path = self.root / "private/layout/reader.py"
        path.write_text(source, encoding="utf-8")
        cfg = read_json(self.layout_config)
        cfg["layout"]["bridge"] = {"reader": "private/layout/reader.py:read"}
        write_json(self.layout_config, cfg)
        return prepare_layout(self.source)

    def test_no_stdout_channel_and_private_diagnostic(self):
        run = self.reader_run("def read(context):\n    print('private marker')\n    return {'answer': 7}\n")
        self.assertEqual(read_placement(run, 10), {"answer": 7})
        logs = list((run / "private_logs").iterdir())
        self.assertEqual(len(logs), 1)
        self.assertIn("private marker", logs[0].read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        result = list((run / "reader_results").iterdir())[0]
        self.assertEqual(read_json(result)["result"], {"answer": 7})

    def test_stub_error_only_references_private_log(self):
        run = self.reader_run("def read(context):\n    raise NotImplementedError('PRIVATE CONTENT')\n")
        with self.assertRaises(ReaderInputError) as raised:
            read_placement(run, 10)
        self.assertIn("not implemented", str(raised.exception))
        self.assertNotIn("PRIVATE CONTENT", str(raised.exception))
        log = run / raised.exception.diagnostic_log
        self.assertIn("PRIVATE CONTENT", log.read_text(encoding="utf-8"))

    def test_shadow_package_and_old_python_fallback(self):
        evil = self.root / "analog_agent"
        evil.mkdir()
        (evil / "__init__.py").write_text("raise RuntimeError('SHADOWED')\n", encoding="utf-8")
        run = self.reader_run("def read(context): return {'safe': True}\n")
        with patch.dict(os.environ, {"PYTHONPATH": str(self.root)}):
            self.assertEqual(read_placement(run, 10), {"safe": True})
            with patch("analog_agent.layout_bridge.sys.version_info", (3, 10)):
                self.assertEqual(read_placement(run, 10), {"safe": True})

    def test_second_communicate_timeout_is_always_timeout_error(self):
        run = self.reader_run("def read(context): return {}\n")
        with patch("analog_agent.layout_bridge.subprocess.Popen") as factory, \
                patch("analog_agent.layout_bridge._kill_reader"):
            factory.return_value.communicate.side_effect = subprocess.TimeoutExpired("reader", 1)
            with self.assertRaises(TimeoutError) as raised:
                read_placement(run, 1)
        self.assertEqual(factory.return_value.communicate.call_count, 2)
        self.assertIn("private_logs/", str(raised.exception))

    def test_result_exclusive_create_does_not_overwrite(self):
        from analog_agent.layout_bridge import main
        run = self.reader_run("def read(context): return {}\n")
        result = run / "reader_results/result.json"
        result.parent.mkdir()
        result.write_text("protected", encoding="utf-8")
        with patch.object(sys, "argv", ["reader", str(run), str(result)]):
            with self.assertRaises(FileExistsError):
                main()
        self.assertEqual(result.read_text(encoding="utf-8"), "protected")
