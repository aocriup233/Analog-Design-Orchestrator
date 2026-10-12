"""All diagnostics run against disposable fixtures, without a simulator/AI."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from analog_agent import doctor
from analog_agent.core import write_json


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ado-doctor-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        (self.repo / ".gitignore").write_text("private/\nruns/\ncampaigns/\nlayouts/\n", encoding="utf-8")
        self.track(".gitignore")
        self.root = self.base / "project"
        self.root.mkdir()

    def track(self, *names):
        subprocess.run(["git", "-C", str(self.repo), "add", "--", *names], check=True, capture_output=True)

    def scanner(self):
        with patch("analog_agent.doctor.shutil.which", return_value=None):
            return doctor.secrets(self.repo)

    def test_secrets_pass_ignores_untracked(self):
        (self.repo / "untracked.txt").write_text("-----BEGIN " + "PRIVATE KEY-----", encoding="utf-8")
        self.assertEqual(self.scanner()["status"], "PASS")

    def test_secret_types_fail_without_disclosing_values(self):
        token = "gh" + "p_" + "a" * 30
        credential_value = "test-" + "sensitive-value"
        examples = ["-----BEGIN RSA " + "PRIVATE KEY-----", token,
                    'pass' + 'word="' + credential_value + '"', "user:" + credential_value + "@host:1234",
                    'pass' + 'word=' + credential_value]
        for value in examples:
            with self.subTest(value=value[:8]):
                (self.repo / "bad.txt").write_text(value, encoding="utf-8")
                self.track("bad.txt")
                report = self.scanner()
                self.assertEqual(report["status"], "FAIL")
                self.assertNotIn(value, json.dumps(report))

    def test_missing_each_ignore_fails(self):
        for missing in ("private", "runs", "campaigns", "layouts"):
            (self.repo / ".gitignore").write_text("".join(f"{x}/\n" for x in ("private", "runs", "campaigns", "layouts") if x != missing))
            self.assertEqual(self.scanner()["status"], "FAIL")

    def test_negated_ignore_is_not_coverage(self):
        (self.repo / ".gitignore").write_text("private/\nruns/\ncampaigns/\nlayouts/\n!private/\nprivate/*\n!private/doctor-probe\n")
        self.assertEqual(self.scanner()["status"], "FAIL")

    def test_gitleaks_redacted_stdin_tracked_only(self):
        original = doctor._command
        calls = []
        def fake(argv, **kwargs):
            if argv[0] == "gitleaks":
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 1, b"never forward secrets", b"")
            return original(argv, **kwargs)
        with patch("analog_agent.doctor.shutil.which", return_value="gitleaks"), patch("analog_agent.doctor._command", side_effect=fake):
            report = doctor.secrets(self.repo)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(calls), 1)
        self.assertIn("--redact", calls[0][0])
        self.assertNotIn("never forward secrets", json.dumps(report))

    def test_gitleaks_tool_error_not_pass(self):
        original = doctor._command
        with patch("analog_agent.doctor.shutil.which", return_value="gitleaks"), patch("analog_agent.doctor._command", side_effect=lambda argv, **kw: subprocess.CompletedProcess(argv, 2, b"", b"private error") if argv[0] == "gitleaks" else original(argv, **kw)):
            self.assertEqual(doctor.secrets(self.repo)["status"], "FAIL")

    def test_allowlist_all_real_rejections(self):
        report = doctor.allowlists()
        self.assertEqual(report["status"], "PASS", report)
        self.assertGreaterEqual(len(report["cases"]), 20)

    def test_allowlist_regression_each_family(self):
        for target in ("analog_agent.plugins.load_function", "analog_agent.parameters.validate_value",
                       "analog_agent.backends.validate_spectre_cmd", "analog_agent.layout_executor.validate_operation"):
            with self.subTest(target=target), patch(target, return_value=None):
                self.assertEqual(doctor.allowlists()["status"], "FAIL")

    def test_forbidden_bridge_registry_regression(self):
        from analog_agent.layout_executor import OPERATIONS
        with patch("analog_agent.layout_executor.OPERATIONS", OPERATIONS | {"execute_skill"}):
            self.assertEqual(doctor.allowlists()["status"], "FAIL")

    def test_dispatch_rejects_before_context_or_bridge(self):
        from analog_agent.layout_executor import configured
        with patch("analog_agent.layout_executor.layout_context") as context:
            with self.assertRaises(ValueError):
                configured(self.root, "execute_skill")
            context.assert_not_called()

    def test_unexpected_probe_exception_not_verified(self):
        with patch("analog_agent.plugins.load_function", side_effect=FileNotFoundError):
            self.assertEqual(doctor.allowlists()["status"], "FAIL")

    def test_paths_confinement_actual_writers(self):
        report = doctor.paths()
        self.assertIn(report["status"], {"PASS", "WARN"}, report)
        self.assertGreaterEqual(report["probes"], 10)

    def test_paths_detect_broken_shared_validator(self):
        with patch("analog_agent.core.local_path", side_effect=lambda root, value: (root / value).resolve()):
            report = doctor.paths()
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any(f["reason"] == "outside tree changed" for f in report["findings"]))

    def test_paths_detect_each_writer_guard_regression(self):
        # Direct waveform/memory callback regression, independent of shared core.
        for target in ("analog_agent.waveforms.local_path", "analog_agent.memory.local_path"):
            with self.subTest(target=target), patch(target, side_effect=lambda root, value: (root / value).resolve()):
                self.assertEqual(doctor.paths()["status"], "FAIL")

    def rules(self):
        rule = self.base / "rules"
        rule.mkdir()
        # Split across a line; location is the first word's original line.
        text = "uniquealpha uniquebeta uniquegamma uniquedelta\nuniqueepsilon uniquezeta uniqueeta uniquetheta uniqueninth\n"
        (rule / "deck").write_text(text, encoding="utf-8")
        write_json(self.root / "private/layout/config.json", {"layout": {"drc": {"rule_dir": str(rule)}, "lvs": {"rule_dir": str(rule)}}})
        return text

    def test_pdk_missing_config_not_applicable(self):
        self.assertFalse(doctor.pdk(self.root)["applicable"])

    def test_pdk_clean(self):
        self.rules()
        (self.root / "safe.log").write_text("unrelated output", encoding="utf-8")
        self.assertEqual(doctor.pdk(self.root)["status"], "PASS")

    def test_pdk_context_plan_log_and_runset_leaks_redacted(self):
        text = self.rules()
        for name in ("layouts/l1/routing_proposals/p/context.json", "runs/r1/config.json", "campaigns/c1/cycle.json", "logs/tool.log", "routing_plan.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("header\n" + text, encoding="utf-8")
        report = doctor.pdk(self.root)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len({f["file"] for f in report["findings"]}), 5)
        self.assertNotIn("uniquealpha", json.dumps(report))
        self.assertTrue(all(set(f) == {"file", "line"} for f in report["findings"]))

    def test_pdk_explicit_role_inputs(self):
        text = self.rules()
        (self.root / "custom.txt").write_text(text)
        write_json(self.root / "project.json", {"doctor": {"role_input_files": ["custom.txt"]}})
        self.assertEqual(doctor.pdk(self.root)["status"], "FAIL")

    def test_pdk_custom_role_requires_inventory(self):
        self.rules()
        write_json(self.root / "project.json", {"workflow": {"role_commands": {"routing": ["never-execute"]}}})
        self.assertEqual(doctor.pdk(self.root)["status"], "FAIL")
        write_json(self.root / "project.json", {"workflow": {"role_commands": {"routing": ["never-execute"]}}, "doctor": {"role_input_files": []}})
        self.assertEqual(doctor.pdk(self.root)["status"], "PASS")

    def test_pdk_runset_source(self):
        self.rules()
        runset = self.base / "runset"
        runset.write_text("one two three four five six seven eight")
        cfg = json.loads((self.root / "private/layout/config.json").read_text())
        cfg["layout"]["drc"]["runset"] = str(runset)
        write_json(self.root / "private/layout/config.json", cfg)
        (self.root / "tool.log").write_text(runset.read_text())
        self.assertEqual(doctor.pdk(self.root)["status"], "FAIL")

    def test_dependencies_missing_lock(self):
        self.assertEqual(doctor.dependencies(self.repo)["status"], "FAIL")

    def test_dependencies_empty_core_lock_dry_run(self):
        (self.repo / "requirements.lock").write_text("# zero runtime dependencies\n")
        with patch("analog_agent.doctor._command", return_value=subprocess.CompletedProcess([], 0)) as command:
            report = doctor.dependencies(self.repo)
        self.assertEqual(report["status"], "PASS")
        argv = command.call_args.args[0]
        for flag in ("--dry-run", "--require-hashes", "--no-index", "--only-binary=:all:", "--no-cache-dir"):
            self.assertIn(flag, argv)
        self.assertFalse(report["installed"])

    def test_dependencies_missing_hash_and_executable_inputs(self):
        for requirement in ("numpy==1.2", "-e .", "./malicious.tar.gz", "https://example.invalid/pkg", "--extra-index-url https://example.invalid"):
            (self.repo / "requirements.lock").write_text(requirement)
            with patch("analog_agent.doctor._command") as command:
                self.assertEqual(doctor.dependencies(self.repo)["status"], "FAIL")
                command.assert_not_called()

    def test_dependencies_unavailable_offline_fails(self):
        (self.repo / "requirements.lock").write_text("package==1.2 --hash=sha256:" + "a" * 64)
        with patch("analog_agent.doctor._command", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(doctor.dependencies(self.repo)["status"], "FAIL")

    def test_obsolete_zero_dependency_lock_fails(self):
        (self.repo / "requirements.lock").write_text("# no dependencies")
        (self.repo / "pyproject.toml").write_text('[project]\ndependencies = ["somepkg"]\n')
        self.assertEqual(doctor.dependencies(self.repo)["status"], "FAIL")

    def test_private_log_and_lock_permission_failures_all_hosts(self):
        log = self.root / "private_logs/tool.log"
        log.parent.mkdir()
        log.write_text("private")
        (self.root / ".layout.lock").write_text("{}")
        with patch("analog_agent.doctor._permissions", return_value=(False, False)):
            report = doctor.permissions(self.root, self.repo)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(report["findings"]), 4)

    @unittest.skipUnless(os.name == "nt", "Windows ACL inspection")
    def test_windows_acl_restricted_and_broad(self):
        for private in (True, False):
            with patch("analog_agent.doctor._command", return_value=subprocess.CompletedProcess([], 0, json.dumps({"private": private, "owned": False}).encode(), b"")):
                self.assertEqual(doctor._permissions(self.root / "anything", True, True), (private, False))

    def test_private_configuration_in_repo_fails(self):
        (self.repo / "private").mkdir()
        self.assertEqual(doctor.permissions(self.repo, self.repo)["status"], "FAIL")

    def test_private_user_project_allowed(self):
        (self.root / "private").mkdir()
        self.assertEqual(doctor.permissions(self.root, self.repo)["status"], "PASS")

    @unittest.skipIf(os.name == "nt", "POSIX mode/uid checks; Windows ACLs tested separately")
    def test_private_log_wrong_mode_fails(self):
        log = self.root / "private_logs/tool.log"
        log.parent.mkdir()
        log.write_text("private diagnostics")
        log.chmod(0o644)
        self.assertEqual(doctor.permissions(self.root, self.repo)["status"], "FAIL")
        log.chmod(0o600)
        self.assertEqual(doctor.permissions(self.root, self.repo)["status"], "PASS")

    @unittest.skipIf(os.name == "nt", "POSIX ownership fixture")
    def test_wrong_lock_owner_fails(self):
        lock = self.root / ".layout.lock"
        lock.write_text("{}")
        with patch("analog_agent.doctor.os.getuid", return_value=lock.stat().st_uid + 1):
            self.assertEqual(doctor.permissions(self.root, self.repo)["status"], "FAIL")

    def test_stale_locks_and_orphans_report_never_cleanup(self):
        lock = self.root / ".layout.lock"
        write_json(lock, {"pid": 987654321, "host": socket.gethostname(), "argv": ["do-not-disclose"], "started_at": "old"})
        before = doctor._inventory(self.root)
        with patch("analog_agent.doctor.processes", return_value=[{"pid": 2, "ppid": 1, "name": "spectre", "reader": False}]):
            report = doctor.stale(self.root)
        self.assertEqual(report["status"], "WARN")
        self.assertEqual(report["findings"][0]["state"], "STALE")
        self.assertEqual(len(report["orphan_processes"]), 1)
        self.assertFalse(report["cleaned"])
        self.assertEqual(before, doctor._inventory(self.root))
        self.assertNotIn("do-not-disclose", json.dumps(report))

    def test_active_foreign_and_malformed_locks(self):
        write_json(self.root / ".layout.lock", {"pid": 3, "host": socket.gethostname()})
        write_json(self.root / ".campaign.lock", {"pid": 3, "host": "other-host"})
        (self.root / "memory.lock").write_text("1")
        with patch("analog_agent.doctor.processes", return_value=[{"pid": 3, "ppid": 2, "name": "python", "reader": False}]):
            report = doctor.stale(self.root)
        self.assertEqual({f["state"] for f in report["findings"]}, {"ACTIVE", "UNKNOWN_OWNER"})

    def test_dependency_lock_is_not_task_lock(self):
        (self.root / "requirements.lock").write_text("# deterministic dependency lock")
        with patch("analog_agent.doctor.processes", return_value=[]):
            self.assertEqual(doctor.stale(self.root)["status"], "PASS")
        self.assertEqual(doctor.permissions(self.root, self.repo)["status"], "PASS")

    def test_real_campaign_and_custom_memory_lock_names(self):
        lock = self.root / "campaigns/c1/.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("1")
        memory = self.root / "private/custom.blob.lock"
        memory.parent.mkdir(parents=True)
        memory.write_text("1")
        with patch("analog_agent.doctor.processes", return_value=[]):
            report = doctor.stale(self.root)
        self.assertEqual(len(report["findings"]), 2)
        self.assertTrue(all(f["state"] == "UNKNOWN_OWNER" for f in report["findings"]))

    def test_diagnose_never_mutates_inspected_trees_or_runs_hooks(self):
        write_json(self.root / "project.json", {"workflow": {"role_commands": {"netlist": ["never-execute"]}}})
        (self.repo / "requirements.lock").write_text("# empty core")
        before = doctor._inventory(self.root)
        with patch("analog_agent.doctor.processes", return_value=[]), patch("analog_agent.plugins.load_function", wraps=__import__("analog_agent.plugins", fromlist=["load_function"]).load_function) as plugin, patch("analog_agent.doctor.shutil.which", return_value=None):
            report = doctor.diagnose(self.root, self.repo)
        self.assertEqual(before, doctor._inventory(self.root))
        self.assertFalse(any("never-execute" in str(c) for c in plugin.call_args_list))
        self.assertNotEqual(report["status"], "FAIL", report)

    def test_unavailable_check_fails_without_sensitive_exception(self):
        with patch("analog_agent.doctor.secrets", side_effect=ValueError("private rule text")), patch("analog_agent.doctor.processes", return_value=[]):
            report = doctor.diagnose(self.root, self.repo)
        self.assertEqual(report["status"], "FAIL")
        self.assertNotIn("private rule text", json.dumps(report))
        self.assertEqual(len(report["checks"]), 7)

    def test_cli_json_exit_code_and_read_only(self):
        from analog_agent.cli import main
        report = {"status": "FAIL", "checks": [{"check": "probe", "status": "FAIL", "findings": []}]}
        before = doctor._inventory(self.root)
        with patch("sys.argv", ["analog-agent", "doctor", "--json"]), patch("analog_agent.doctor.diagnose", return_value=report), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue()), report)
        self.assertEqual(before, doctor._inventory(self.root))

    def test_cli_text_pass(self):
        from analog_agent.cli import main
        with patch("sys.argv", ["analog-agent", "doctor"]), patch("analog_agent.doctor.diagnose", return_value={"status": "PASS", "checks": [{"check": "probe", "status": "PASS", "findings": []}]}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertIn("PASS: probe", output.getvalue())


if __name__ == "__main__":
    unittest.main()
