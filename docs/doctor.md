# S1: read-only safety doctor

Run `analog-agent doctor [--json]` in the user project. It never invokes an
LLM, private plugin, role command, Bridge, simulator, scheduler or remote shell.
The JSON report has seven checks, locations/categories only, and exits 1 on
any FAIL. WARN is report-only, not proof of safety. No lock is reclaimed and no
job is resubmitted. S2 monitoring/hash gates and S3 retries are not implemented.

The checkout hosting the installed package is the public repository under test;
the current directory is the user project. A source checkout and Git are needed
for tracked-file diagnostics. Installations without that evidence fail closed.
Run with `PYTHONDONTWRITEBYTECODE=1` / `python -B` to suppress Python's own import
cache writes. Doctor writes only in automatically removed OS temporary fixtures;
it does not write reports, logs, configs or receipts in the inspected trees.

| Check | Evidence / limits |
|---|---|
| Secrets | Worktree bytes of Git-tracked files only. Installed gitleaks receives each file through redacted stdin; tool errors FAIL. Otherwise conservative key/token/quoted-password/credentialed-host scanner. Findings never contain a value. `.gitignore` must cover private/runs/campaigns/layouts; global ignores are not accepted as policy. |
| Allowlists | Fixed rejection probes call existing plugin/parameter/executable/typed-operation validators. Forbidden callables are never invoked; only a harmless temporary local function can be imported. Unknown exceptions do not certify rejection. |
| Paths | Parent and outside absolute destinations for all five writer families use their shared path contract; actual run/campaign/layout creators have fixed storage names, so their storage symlink attacks exercise the real creators. Waveform saving and memory submission are exercised directly. An outside sentinel/tree hash catches writes even when an exception follows. Unavailable symlink privilege is a WARN with explicitly skipped probes, not a PASS. |
| PDK leaks | Configured layout DRC/LVS directories and runsets supply SHA-256 8-word shingle digests, never copied rule text. Whitespace-token shingles span lines. Scan a conservative superset: all run/campaign/layout artifacts, context/plan files, and every `.log`/`.out` anywhere in the project. Private source decks themselves are excluded. Additional project-local role input files must be declared in `project.json` as `doctor.role_input_files`. With configured PDK rules, custom role hooks lacking this explicit inventory FAIL (declare `[]` if genuinely none); private overrides are included. External/custom role-hook inputs cannot be inferred from arbitrary executable code: deployments must audit their declarations. Missing/unreadable configured sources or symlinked rule sources without an audited inventory FAIL. Matches report file/line only. This is exact shingle leakage detection, not semantic confidentiality proof. |
| Dependencies | `requirements.lock` pins the currently dependency-free **core runtime**, not optional analysis/BO/Bridge extras or build tools. Fail if core dependencies change without extending lock coverage. `pip install --dry-run --require-hashes --ignore-installed --only-binary=:all: --no-index --no-cache-dir --no-deps` checks without installing, contacting an index, executing source build hooks or mutating cache. Non-empty locks whose wheels are unavailable offline FAIL rather than claiming installability. Deployment extras need separate audited hash locks. |
| Permissions | Private files in the user project's Git index FAIL, even if ignored or deleted locally. An ignored, untracked private tree only WARNs; untracked but unignored trees WARN to request ignore coverage. Private data is allowed inside the project/repository directory. Projects without Git retain filesystem permission checks. POSIX diagnostic logs require exactly 0600, lock UID must be current UID. Windows checks actual ACL SIDs: only current user, SYSTEM and Administrators may have Allow entries on private logs; lock owner must be current user. ACL-inspection errors FAIL. This checks files, not remote hosts or every ancestor ACL. |
| Stale state | Read-only local process snapshot and lock owner pid/host. Foreign/legacy malformed owners are UNKNOWN_OWNER, not reclaimed. Parentless Spectre/ADO reader processes are potential orphans, not proof of their origin; no command lines/argv are printed. Remote scheduler/orphan visibility is outside S1. Warnings never trigger cleanup. |

`requirements-ci.lock` separately pins the Linux x86_64 / Python 3.12 test-only
plotting wheels and all transitive dependencies, using PyPI SHA-256 metadata.
CI runs the lean doctor first, then explicitly installs this hash-checked test
lock to run the existing waveform tests. That CI setup is not a doctor action;
optional BO/Bridge deployments are still outside the core-lock claim.

Linux CI runs `doctor --json --require-symlinks`: any skipped symlink probe
is a FAIL, not a WARN. A Linux test also requires all 15 probes and an empty
skipped list. Normal Windows invocations retain explicit WARNs for missing
symlink privileges; the strict flag is available on every platform.

PDK fingerprints additionally hash each nonempty, non-comment rule line after
Unicode case folding and whitespace normalization. All identifier/numeric-token
pairs on a line are hashed too, catching a layer/value excerpt even when the
rule keyword is missing. No foundry-specific identifier list is embedded.
The same hashes are computed for artifacts/logs; outputs still contain only
file/line, never fingerprints or rule text. Identifier/value pairs are deliberately
conservative and may flag coincidental matches; this is not semantic analysis.
Numeric spelling and units are not converted; encoded/reformatted values may
escape detection. Single-word lines can cause false positives. Original 8-word
shingles remain active, including cross-line matches.

The doctor is diagnostic, not a sandbox or repair tool. Trusted arbitrary Python
plugins/configured executables still require independent audit. Keep private
configs ignored and untracked. CI runs doctor before core
unit tests, without installing optional EDA/AI packages or contacting AI services.
