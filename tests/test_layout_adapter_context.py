"""ADO-02: typed adapters resolve paths from the verified run, not cwd."""

import contextlib
import io
from unittest.mock import patch

from analog_agent.core import read_json, write_json
from analog_agent.layout_adapter_context import adapter_context, resolve_run_path, validate_adapter_context
from analog_agent.layout_executor import execute
from analog_agent.layout_bridge import read_placement
from test_layout_floorplan import LayoutFloorplanFixture


class LayoutAdapterContextTest(LayoutFloorplanFixture):
    def setUp(self):
        super().setUp()
        adapter = self.root / "private/layout/operations.py"
        adapter.write_text(
            "from analog_agent.layout_adapter_context import resolve_run_path\n"
            "def dispatch(request):\n"
            "    path = resolve_run_path(request['adapter_context'], request['payload']['destination'])\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    path.write_text('CDL', encoding='utf-8')\n"
            "    return {'destination': str(path), 'context': request['adapter_context']}\n",
            encoding="utf-8")
        config = read_json(self.layout_config)
        config["layout"]["lvs_source"] = {
            "generator": "private/layout/operations.py:dispatch",
            "normalizer": "private/layout/operations.py:dispatch",
            "top": "top", "dependencies": []}
        write_json(self.layout_config, config)
        from analog_agent.layout_runs import prepare_layout
        self.run = prepare_layout(self.source)

    def test_isolated_child_resolves_run_relative_lvs_path(self):
        value = execute(self.run, "generate_lvs_source", {"destination": "lvs_sources/fixture/source.cdl"})
        destination = self.run / "lvs_sources/fixture/source.cdl"
        self.assertEqual(destination.read_text(encoding="utf-8"), "CDL")
        self.assertEqual(value["destination"], str(destination))
        self.assertEqual(value["context"]["run_root"], str(self.run))
        execution = next((self.run / "execution").iterdir())
        self.assertEqual(value["context"]["request_dir"], str(execution))
        self.assertEqual(value["context"]["result_dir"], str(execution))

    def test_inconsistent_result_envelope_is_unknown_state(self):
        from analog_agent.layout_bridge import ReaderStateError
        with patch("analog_agent.layout_executor.context_sha256", return_value="0" * 64):
            with self.assertRaisesRegex(ReaderStateError, "inconsistent context"):
                execute(self.run, "generate_lvs_source", {"destination": "lvs_sources/fixture/source.cdl"})

    def test_context_rejects_cross_run_and_inconsistent_dirs(self):
        channel = adapter_context(self.run, self.run / "execution", self.run / "execution")
        other = self.root / "layouts/other"
        other.mkdir()
        write_json(other / "task.json", {"kind": "layout", "run_id": "other"})
        for changed in ({**channel, "run_root": str(other)},
                        {**channel, "request_dir": str(other)},
                        {**channel, "schema_version": 2}):
            with self.assertRaises(ValueError):
                validate_adapter_context(changed, self.run, self.run / "execution", self.run / "execution")
        for relative in ("../other/task.json", str(other / "task.json"), "\\escape", "./x", "C:/other/file"):
            with self.assertRaises(ValueError):
                resolve_run_path(channel, relative)

    def test_child_refuses_forged_request_before_plugin_load(self):
        from analog_agent.layout_executor import main
        directory = self.run / "execution/forged"
        directory.mkdir(parents=True)
        request, result = directory / "request.json", directory / "result.json"
        forged = adapter_context(self.run, directory, directory)
        forged["request_dir"] = str(self.root)
        write_json(request, {"operation": "generate_lvs_source", "payload": {"destination": "wrong.cdl"},
                             "adapter_context": forged})
        with patch("sys.argv", ["layout_executor", str(self.run), str(request), str(result)]), \
             patch("analog_agent.layout_executor.load_function") as load, \
             contextlib.redirect_stderr(io.StringIO()):
            main()
        load.assert_not_called()
        self.assertEqual(read_json(result)["status"], "crash")
        self.assertFalse((self.run / "wrong.cdl").exists())

    def test_symlink_escape_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.run / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        channel = adapter_context(self.run, self.run, self.run)
        with self.assertRaises(ValueError):
            resolve_run_path(channel, "escape/secret.cdl")

    def test_reader_receives_same_versioned_path_contract(self):
        reader = self.root / "private/layout/reader.py"
        reader.write_text(
            "from analog_agent.layout_adapter_context import resolve_run_path\n"
            "def read(context):\n"
            "    p = resolve_run_path(context['adapter_context'], 'layout_inputs.json')\n"
            "    return {'path': str(p), 'context': context['adapter_context']}\n",
            encoding="utf-8")
        config = read_json(self.layout_config)
        config["layout"]["bridge"] = {"reader": "private/layout/reader.py:read"}
        write_json(self.layout_config, config)
        from analog_agent.layout_runs import prepare_layout
        run = prepare_layout(self.source)
        result = read_placement(run, 10)
        self.assertEqual(result["path"], str(run / "layout_inputs.json"))
        self.assertEqual(result["context"]["schema_version"], 1)
        self.assertEqual(result["context"]["request_dir"], str(run / "reader_results"))
