"""Read-only, offline safety diagnostics. Never run project plugins or role hooks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile


def result(name, findings=(), *, status=None, **details):
    return {"check": name, "status": status or ("FAIL" if findings else "PASS"),
            "findings": list(findings), **details}


def _command(argv, *, cwd=None, data=None, timeout=30):
    return subprocess.run(argv, cwd=cwd, input=data, capture_output=True,
                          timeout=timeout, check=False)


def tracked_files(repo):
    reply = _command(["git", "-C", str(repo), "ls-files", "-z"])
    if reply.returncode:
        raise ValueError("Tracked-file inventory unavailable")
    from .core import local_path
    return [local_path(repo, os.fsdecode(name)) for name in reply.stdout.split(b"\0") if name]


# Return locations/categories only; never include matched values, tool output or traces.
_SECRETS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,})\b")),
    ("credential", re.compile(r"(?i)\b(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret[_-]?key)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{4,})[\"']")),
    ("credential", re.compile(r"(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token)\b\s*=\s*([A-Za-z0-9][A-Za-z0-9_.+-]{7,})(?=\s|$)")),
    ("credential-url", re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://)?[^\s/:@]+:[^\s/@]+@[^\s/:]+:\d+")),
)
_PLACEHOLDER = re.compile(r"(?i)^(?:<.*>|\$.*|YOUR[_-].*|USER[_-].*|CHANGEME|REPLACE[_-].*|placeholder|example|none|null)$")


def secrets(repo):
    findings = []
    files = tracked_files(repo)
    scanner = shutil.which("gitleaks")
    for path in files:
        if not path.is_file():
            findings.append({"file": str(path.relative_to(repo)), "reason": "tracked file unavailable"})
            continue
        content = path.read_bytes()
        if scanner:
            reply = _command([scanner, "stdin", "--redact", "--no-banner"], data=content)
            if reply.returncode:
                findings.append({"file": str(path.relative_to(repo)), "reason":
                                 "gitleaks finding" if reply.returncode == 1 else "gitleaks check failed"})
        else:
            for line, text in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
                for kind, pattern in _SECRETS:
                    match = pattern.search(text)
                    if match and not (kind == "credential" and _PLACEHOLDER.fullmatch(match[1])):
                        findings.append({"file": str(path.relative_to(repo)), "line": line, "kind": kind})
    # --no-index excludes .git/info/exclude and global ignores; require repository policy.
    for directory in ("private", "runs", "campaigns", "layouts"):
        reply = _command(["git", "-C", str(repo), "-c", "core.excludesFile=",
                          "check-ignore", "--no-index", "-v", "--", directory + "/doctor-probe"])
        if reply.returncode or b".gitignore:" not in reply.stdout or b":!" in reply.stdout.split(b"\t", 1)[0]:
            findings.append({"file": ".gitignore", "reason": f"missing {directory}/ coverage"})
    return result("secrets", findings, scanner="gitleaks" if scanner else "builtin", tracked=len(files))


def allowlists():
    from .plugins import load_function
    from .parameters import validate_value
    from .backends import validate_spectre_cmd
    from .layout_executor import validate_operation
    findings, tested = [], []
    with tempfile.TemporaryDirectory(prefix="ado-doctor-allowlist-") as temporary:
        root = Path(temporary) / "project"
        root.mkdir()
        outside = Path(temporary) / "outside.py"
        outside.write_text("def harmless(): return None\n", encoding="utf-8")
        probes = []
        for spec in ("os:system", "subprocess:run", "builtins:eval", "../outside.py:harmless"):
            probes.append(("plugin:" + spec, lambda spec=spec: load_function(spec, root,
                           {"plugins": {"allowed_modules": ["os", "subprocess", "builtins"]}})))
        for value in ("1 2", '1"2', "1'2", "1\\\n2", "1//2", "include"):
            probes.append(("parameter:" + repr(value), lambda value=value: validate_value("P", value)))
        for command in ("sh", "./spectre", "spectre;echo", "spectre -64", "$(spectre)"):
            probes.append(("spectre:" + command, lambda command=command: validate_spectre_cmd({"spectre_cmd": command})))
        for operation in ("execute_skill", "load_il", "run_shell_command", "system()", "sh()", "eval", "read_placement;sh()"):
            probes.append(("bridge:" + operation, lambda operation=operation: validate_operation(operation)))
        for name, probe in probes:
            tested.append(name)
            try:
                probe()
            except ValueError:
                pass
            except Exception:
                findings.append({"case": name, "reason": "unexpected exception; rejection unverified"})
            else:
                findings.append({"case": name, "reason": "accepted forbidden input"})
    return result("allowlists", findings, cases=tested)


def _fixture(root):
    from .core import write_json
    root.mkdir(parents=True)
    (root / "template.scs").write_text("// deterministic confinement fixture\n", encoding="utf-8")
    config = {"template": "template.scs", "parameters": {}, "metrics": {"v": {"kind": "scalar", "key": "v"}},
              "rules": {"v": {"min": 0}}, "simulation": {"backend": "external"}}
    write_json(root / "project.json", config)
    return root / "project.json"


def _inventory(root):
    return {str(p.relative_to(root)): ("directory" if p.is_dir() else
            (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns))
            for p in root.rglob("*")}


def paths():
    """Probe actual writers exclusively in disposable projects; never user hooks."""
    from .core import local_path, create_run, write_json
    from .campaign import create_campaign
    from .layout_runs import _atomic_run
    from .waveforms import _output_path, _save_plot
    from .memory import submit_candidate
    findings, skipped, count = [], [], 0
    class Figure:
        def savefig(self, stream, **options):
            stream.write(b"probe")
    with tempfile.TemporaryDirectory(prefix="ado-doctor-paths-") as temporary:
        base = Path(temporary)
        outside = base / "outside"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"unchanged")
        for writer in ("run", "campaign", "layout", "waveform", "memory"):
            for attack in ("parent", "absolute", "symlink"):
                root = base / f"{writer}-{attack}"
                cfg_path = _fixture(root)
                bad = "../outside/escape.csv" if attack == "parent" else str(outside / "escape.csv")
                if attack == "symlink":
                    name = {"run": "runs", "campaign": "campaigns", "layout": "layouts"}.get(writer, "escape")
                    try:
                        (root / name).symlink_to(outside, target_is_directory=True)
                    except OSError:
                        skipped.append({"writer": writer, "attack": attack, "reason": "symlink unavailable"})
                        continue
                    bad = f"{name}/escape.csv"
                before = _inventory(outside)
                count += 1
                try:
                    if writer in {"run", "campaign", "layout"} and attack != "symlink":
                        # Fixed-directory creators expose no user destination argument;
                        # exercise their shared destination validator before writing.
                        destination = local_path(root, bad)
                        write_json(destination, {"probe": True})
                    elif writer == "run":
                        create_run(cfg_path, 0)
                    elif writer == "campaign":
                        create_campaign(cfg_path)
                    elif writer == "layout":
                        _atomic_run(root, {})
                    elif writer == "waveform":
                        options = {"output": bad}
                        _save_plot(Figure(), _output_path(root, options, "output"), options)
                    else:
                        private = root / "private"
                        private.mkdir()
                        write_json(private / "candidate.json", {"id": "probe", "kind": "design", "summary": "probe",
                                                               "scope": {}, "evidence": ["template.scs"]})
                        submit_candidate({"_project_root": str(root), "memory": {"path": bad}}, private / "candidate.json")
                except ValueError:
                    pass
                except Exception:
                    findings.append({"writer": writer, "attack": attack, "reason": "unexpected exception; confinement unverified"})
                else:
                    findings.append({"writer": writer, "attack": attack, "reason": "escape accepted"})
                if _inventory(outside) != before:
                    findings.append({"writer": writer, "attack": attack, "reason": "outside tree changed"})
    return result("paths", findings, status="FAIL" if findings else ("WARN" if skipped else "PASS"),
                  probes=count, skipped=skipped)


def _files(root):
    """No traversal of symlinked directories or external files."""
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".venv"}
                         and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if not path.is_symlink():
                yield path


def _shingles(path):
    words = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line, text in enumerate(stream, 1):
            for token in re.findall(r"\S+", text):
                words.append((token, line))
                if len(words) == 8:
                    yield hashlib.sha256(" ".join(w for w, _ in words).encode()).digest(), words[0][1]
                    words.pop(0)


def pdk(root):
    from .core import local_path
    source = local_path(root, "private/layout/config.json")
    if not source.exists():
        return result("pdk", status="PASS", applicable=False, reason="no configured layout rule/runset files")
    from .core import read_json
    cfg = read_json(source)["layout"]
    hashes, rule_files = set(), set()
    for stage in ("drc", "lvs"):
        options = cfg[stage]
        directory = Path(options["rule_dir"])
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("Rule source unavailable")
        for parent, dirs, names in os.walk(directory, followlinks=False):
            if any((Path(parent) / name).is_symlink() for name in dirs + names):
                raise ValueError("Symlinked rule sources need an explicit audited inventory")
        rule_files.update(_files(directory))
        if options.get("runset"):
            rule_files.add(Path(options["runset"]))
    for path in sorted(rule_files):
        hashes.update(digest for digest, _ in _shingles(path))
    # Conservative superset: every run/campaign/layout artifact (including nested
    # role contexts and plan inputs), every log anywhere, plus declared role inputs.
    targets = {p for p in _files(root) if p.relative_to(root).parts[0] in {"runs", "campaigns", "layouts", "logs", "private_logs"}
               or p.suffix.lower() in {".log", ".out"} or "context" in p.name.lower() or "plan" in p.name.lower()}
    project = root / "project.json"
    coverage = []
    if project.exists():
        from .core import PRIVATE_OVERRIDES, _merge
        config = read_json(project)
        for name in PRIVATE_OVERRIDES:
            override = local_path(root, name)
            if override.exists():
                _merge(config, read_json(override))
        # Explicit additional input files, not executable role command arguments.
        for name in config.get("doctor", {}).get("role_input_files", []):
            targets.add(local_path(root, name))
        if config.get("workflow", {}).get("role_commands") and "role_input_files" not in config.get("doctor", {}):
            coverage.append({"file": "project.json", "reason": "custom role hooks require an explicit additional-input inventory (empty list if none)"})
    findings = list(coverage)
    for path in sorted(targets - rule_files):
        for digest, line in _shingles(path):
            if digest in hashes:
                findings.append({"file": str(path.relative_to(root)), "line": line})
    return result("pdk", [dict(item) for item in {json.dumps(f, sort_keys=True): f for f in findings}.values()],
                  applicable=True, rule_files=len(rule_files), scanned_files=len(targets))


def dependencies(repo):
    lock = repo / "requirements.lock"
    if not lock.is_file():
        return result("dependencies", [{"file": "requirements.lock", "reason": "missing hash lock"}])
    # No index, no cache writes, no build/isolation hooks, no installation.
    # Source/editable/local requirements could execute build hooks even in dry-run.
    text = lock.read_text(encoding="utf-8")
    entries = " ".join(line.split("#", 1)[0].strip() for line in text.splitlines()).replace("\\", " ")
    if entries.strip() and not re.fullmatch(r"(?:\s*[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+(?:\s+--hash=sha256:[a-f0-9]{64})+\s*)+", entries):
        return result("dependencies", [{"file": "requirements.lock", "reason": "only pinned versions with SHA-256 hashes allowed"}])
    project = repo / "pyproject.toml"
    if project.exists():
        # The portable core currently declares no runtime dependencies. Fail closed
        # when that changes instead of certifying an obsolete empty lock.
        declaration = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", project.read_text(encoding="utf-8"))
        if not declaration or not re.search(r"(?m)^dependencies\s*=\s*\[\s*\]\s*$", declaration[1]):
            return result("dependencies", [{"file": "pyproject.toml", "reason": "core dependencies changed; regenerate and extend hash-lock coverage"}])
    reply = _command([sys.executable, "-B", "-m", "pip", "--disable-pip-version-check", "install", "--dry-run",
                      "--require-hashes", "--only-binary=:all:", "--ignore-installed", "--no-deps", "--no-index", "--no-cache-dir",
                      "-r", str(lock)], timeout=60)
    return result("dependencies", [] if reply.returncode == 0 else [{"file": "requirements.lock", "reason": "offline pip hash-check dry-run failed"}],
                  scope="core runtime only; optional extras not certified", installed=False)


def _permissions(path, log, lock):
    if os.name != "nt":
        info = path.stat()
        return ((not log or stat.S_IMODE(info.st_mode) == 0o600),
                (not lock or info.st_uid == os.getuid()))
    # Translate identities to stable SIDs, independent of localized Windows names.
    # Exclude inherited broad Allow ACLs too; file-mode bits are not ACL evidence.
    script = ("$ErrorActionPreference='Stop'; "
              "$p=[Console]::In.ReadToEnd()|ConvertFrom-Json; "
              "$acl=Get-Acl -LiteralPath $p; "
              "$me=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
              "$owner=([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value; "
              "$broad=@($acl.Access | Where-Object { $_.AccessControlType -eq 'Allow' -and "
              "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin @($me,'S-1-5-18','S-1-5-32-544') }); "
              "@{private=($broad.Count -eq 0); owned=($owner -eq $me)}|ConvertTo-Json -Compress")
    reply = _command(["powershell", "-NoProfile", "-Command", script], data=json.dumps(str(path)).encode())
    if reply.returncode:
        raise ValueError("ACL verification unavailable")
    value = json.loads(reply.stdout.decode("utf-8-sig"))
    return not log or value["private"] is True, not lock or value["owned"] is True


def _is_lock(path):
    # Dependency locks are reproducibility inputs, not task-owner records.
    return (path.name in {".layout.lock", ".campaign.lock", "memory.lock"} or path.name.endswith(".json.lock")
            or (path.name == ".lock" and "campaigns" in path.parts)
            or (path.name.endswith(".lock") and "private" in path.parts))


def permissions(root, repo):
    findings = []
    private = root / "private"
    if private.exists() and private.resolve().is_relative_to(repo.resolve()):
        findings.append({"file": str(private), "reason": "private config tree inside ADO repository"})
    for path in _files(root):
        parts = path.relative_to(root).parts
        log = "private_logs" in parts or ("execution" in parts and path.name == "stderr.log")
        lock = _is_lock(path)
        if not log and not lock:
            continue
        private_ok, owner_ok = _permissions(path, log, lock)
        if not private_ok:
            findings.append({"file": str(path.relative_to(root)), "reason": "private log not 0600 or equivalent restricted ACL"})
        if not owner_ok:
            findings.append({"file": str(path.relative_to(root)), "reason": "lock owner is not current user"})
    return result("permissions", findings)


def processes():
    """Local process metadata only; never output command lines or credentials."""
    if os.name == "nt":
        reply = _command(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"])
        if reply.returncode:
            raise ValueError("Process inventory unavailable")
        items = json.loads(reply.stdout.decode("utf-8-sig"))
        if isinstance(items, dict):
            items = [items]
        return [{"pid": p["ProcessId"], "ppid": p["ParentProcessId"], "name": p["Name"],
                 "reader": "analog_agent.layout_bridge" in (p["CommandLine"] or "") or "analog_agent.layout_executor" in (p["CommandLine"] or "")}
                for p in items]
    proc = Path("/proc")
    if not proc.is_dir():
        raise ValueError("Process inventory unsupported on this host")
    items = []
    for path in sorted(proc.iterdir()):
        if not path.name.isdigit():
            continue
        try:
            text = (path / "stat").read_text()
            name, tail = text.split("(", 1)[1].rsplit(")", 1)
            command = (path / "cmdline").read_bytes()
            items.append({"pid": int(path.name), "ppid": int(tail.split()[1]), "name": name,
                          "reader": b"analog_agent.layout_bridge" in command or b"analog_agent.layout_executor" in command})
        except (OSError, ValueError):
            continue  # Process exited during the read-only snapshot.
    return items


def stale(root):
    items = processes()
    live = {p["pid"] for p in items}
    findings = []
    for path in _files(root):
        if not _is_lock(path):
            continue
        try:
            owner = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(owner, dict):
                owner = {}
        except (OSError, ValueError):
            owner = {}
        pid, host = owner.get("pid"), owner.get("host")
        known = host == socket.gethostname() and type(pid) is int
        findings.append({"file": str(path.relative_to(root)), "pid": pid if type(pid) is int else None,
                         "host": host if isinstance(host, str) else None,
                         "state": ("ACTIVE" if pid in live else "STALE") if known else "UNKNOWN_OWNER"})
    orphans = [{"pid": p["pid"], "ppid": p["ppid"], "name": p["name"]} for p in items
               if (p["name"].lower().startswith("spectre") or p["reader"])
               and (p["ppid"] <= 1 or p["ppid"] not in live)]
    return result("stale", findings, status="WARN" if findings or orphans else "PASS", orphan_processes=orphans, cleaned=False)


def diagnose(root=None, repo=None):
    root = Path(root or Path.cwd()).resolve()
    repo = Path(repo or Path(__file__).resolve().parents[2]).resolve()
    checks = []
    for name, check in (("secrets", lambda: secrets(repo)), ("allowlists", allowlists), ("paths", paths),
                        ("pdk", lambda: pdk(root)), ("dependencies", lambda: dependencies(repo)),
                        ("permissions", lambda: permissions(root, repo)), ("stale", lambda: stale(root))):
        try:
            checks.append(check())
        except Exception as exc:
            # Never leak private file/adapter/tool content through exception text.
            checks.append(result(name, [{"reason": "check unavailable", "error_type": type(exc).__name__}]))
    return {"schema_version": 1, "read_only": True, "status": "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS",
            "checks": checks}
