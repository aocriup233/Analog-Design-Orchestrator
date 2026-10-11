# Layout L1/L2: configuration, constraints and manual floorplan

L1/L2 is an opt-in framework with fake-backend tests, not a certified live Virtuoso
workflow. [L2 constraints](layout-constraints.md) add reviewed retrieval and
advisory placement checks. L3 routing/DRC/LVS and L4 generator are not implemented.
The [installed Bridge audit](design/layout-bridge-api.md) found no complete typed
placement exporter; L1 requires a separately audited read-only adapter and fails
closed without one. The example adapter deliberately raises NotImplementedError.
There is no raw SKILL, shell, GUI recovery or placement-mutating fallback.

## Private configuration

Copy `private.example/layout/config.json` and `read_placement.py` into the **user
project's** `private/layout/`, not the ADO checkout. Replace all placeholders.
`private/layout/config.json` contains exactly a `layout` object:

| Field | Contract |
|---|---|
| `tool` | `pegasus` or `calibre`; selection only, no verifier is run in L1 |
| `library`, `cell` | Explicit target identifiers; view is `layout` |
| `timeout_s` | Positive finite timeout in seconds, default 300 |
| `drc.rule_dir`, `lvs.rule_dir` | Existing readable/searchable absolute directories outside the project and ADO checkout |
| `drc.run_dir`, `lvs.run_dir` | Same external directory policy; L1 does not create/write in them |
| `drc.runset`, `lvs.runset` | Optional existing readable absolute files outside the project and ADO checkout |
| `bridge.reader` | Trusted function reference, loaded only through `plugins.load_function` |
| `allow_leading_digit` | Boolean, default false; permits a digit at the start of library/cell/instance/master names, never other executable characters |
| `grid_nm` | Required private positive manufacturing grid in nm; no public foundry-specific default |
| `bbox_tolerance_nm` | Nonnegative origin/bbox expansion tolerance in nm, default zero |

Every run directory must be writable and disjoint from **both** rule directories:
equal, inside or containing relationships (including cross-stage relationships)
are rejected after resolution. L3 must create `<run_dir>/<layout_run_id>/` with
`exist_ok=False`, write only in that exclusive directory, and delete only that
exact directory. Never write/delete in a rule directory or shared run root.
Rule/runset/plugin hashes are streamed in 1 MiB chunks. Rejected identifiers are
reported together, listing every offending name rather than stopping at the first.

Paths are validated on the worker's host, not guessed to exist on a remote host.
Execute in the path environment where the configured rule directories exist.
External paths use a dedicated explicit validator; `core.local_path` stays strict
for project inputs, plugins and every run artifact, including symlink resolution.
No rule content is copied into public files, snapshots or prompts.

The `verification_inputs(root, cfg, stage, used_files)` hook returns SHA-256 for
every explicitly consumed rule/runset file, rejecting files outside the rule
directory or the exact configured runset. A configured runset must be listed.
L3 must supply the actual **transitive** input set; directory listing is not proof
of consumption and L1 cannot certify that a future tool reported every include.
There are no verification handoffs yet because L1 executes no verification.

## Run and state contracts

```sh
analog-agent layout prepare SCHEMATIC_RUN_DIR
analog-agent layout status LAYOUT_RUN_DIR
analog-agent layout lock-info LAYOUT_RUN_DIR
analog-agent layout clone LAYOUT_RUN_DIR
analog-agent layout constraints LAYOUT_RUN_DIR
analog-agent layout constraints-freeze LAYOUT_RUN_DIR --reason "reviewed constraints"
analog-agent layout floorplan-export LAYOUT_RUN_DIR
analog-agent layout floorplan-freeze LAYOUT_RUN_DIR --reason "reviewed placement"
analog-agent layout constraints-check LAYOUT_RUN_DIR
analog-agent layout routing-review LAYOUT_RUN_DIR --decision proceed --reason "reviewed clean check"
```

Layout runs live in `<project>/layouts/<id>/`, separately from schematic `runs/`.
Ignore `layouts/` in independent project Git repositories. Preparation requires
a VERIFIED or ANALYZED schematic run and a matching READY netlist/DONE simulation
receipt with no errors. It never changes the schematic's files or status.
An analysis verdict PASS is mandatory. Only an ANALYZED FAIL design can be
explicitly admitted with `--allow-failing-design --reason TEXT`; the verdict,
analysis byte hash and override reason are pinned in `layout_inputs.json`.
Preparation builds `layouts/.tmp-<uuid>/`, fsyncs and atomically renames it to
`layouts/<uuid>/`; failed preparations remove only their owned temporary tree.
Temporary names are never valid runs. Clone revalidates the original inputs,
copies a hash-checked active freeze as **inherited evidence**, records cloned_from
and starts LAYOUT_PREPARED: it does not inherit routing/verification or bypass L2
gates. The original run is never changed; locked/changed sources are refused.
`layout_inputs.json` records schematic run ID, netlist SHA-256, netlist/simulation
receipt hashes, saved public config hash, private layout config byte hash, and
project-local reader file hash. The task pins this input handoff hash. Each
operation rechecks them and the schematic's existing private override manifest.
Private config values are not copied into a public config snapshot or CLI output.
Changing a private layout input requires a new run, not an in-place patch.
Module plugins remain subject to the configured module allowlist; deployments
must separately pin installed adapter and Bridge dependencies/transitive code.

The shared transition table contains:

```text
LAYOUT_PREPARED -> CONSTRAINTS_DRAFTED -> CONSTRAINTS_FROZEN
 -> FLOORPLAN_PENDING -> FLOORPLAN_FROZEN -> ROUTED -> DRC_DONE
 -> LVS_DONE -> LAYOUT_VERIFIED
```

Every step can enter NEEDS_ATTENTION (terminal; inspect/reconcile, then create a
new linked run). Frozen or later states can return to FLOORPLAN_PENDING when
manual placement changes. Schematic states cannot enter layout states. The
presence of a state in the table is **not** an implementation of its worker:
L1/L2 cannot fabricate routing or clean DRC/LVS. Exports before FLOORPLAN_PENDING
are draft snapshots only. Freeze requires L2's hashed constraints human gate;
draft warnings need `--ack-warnings`. Routing checkpoints require a separate
human routing-review (`proceed-with-warnings` when placement warnings remain).
tests use the real draft/freeze commands with fake placement readers. Production
CLI does not bypass missing stages.

Mutating layout commands use an exclusive run lock. A stale `.layout.lock` is
JSON with `pid`, `host`, `argv`, `started_at`; `layout lock-info` only reads it,
even when private inputs have changed. `layout_lock` yields a scoped
LayoutSession capability, invalid after exit. Internal lock-held operations
accept that session instead of re-acquiring the lock; no global re-entrant lock.
The argv owner record is local evidence, not a prompt or public config snapshot;
credentials must never be supplied in command arguments. Locks are
never silently reclaimed: inspect the owning process and reconcile any Bridge
operation before removing that exact lock. Known input errors (missing reader,
unimplemented stub, wrong target, invalid schema/grid/bbox) leave the current
state unchanged. Only pinned-evidence tampering, TimeoutError, or reader crash
with unknown Bridge state enters NEEDS_ATTENTION. Invalid ordering is rejected
without advancing state. Unknown reader failures require reconcile; private
error content is never included in task.json. `layout status` revalidates inputs; inspect
`task.json` directly if changed inputs prevent validation.

## Floorplan schema and hashing

`floorplan.json` has exactly:

```json
{
  "schema_version": 2,
  "source": "manual",
  "cellview": {"library": "USER_LIBRARY", "cell": "USER_CELL", "view": "layout", "path": "USER_LIBRARY/USER_CELL/layout"},
  "units": "nm",
  "export_timestamp": "UTC ISO-8601 timestamp assigned by worker",
  "instances": [],
  "pins": [],
  "labels": [],
  "pr_boundary": []
}
```

Each instance contains exactly `instance` (unique identifier), `master` with
`library`, `cell`, `view`, finite JSON numeric `x`, `y`, `orientation`,
`bbox: [[xmin, ymin], [xmax, ymax]]` in the top-level coordinate frame, `params`
(the complete reported master/PCell JSON parameter object), and `params_sha256`.
The adapter reports the canonical parameter digest; validation recomputes it from
sorted-key compact UTF-8 JSON (`ensure_ascii=False`, `allow_nan=False`). A parameter
change counts even at unchanged xy. Do not infer missing parameters as `{}`.
Pins contain exactly `name`, `layer`, `bbox`; labels contain exactly `text`,
`layer`, `bbox`. `pr_boundary` is an empty list or a polygon of at least three
xy points. Every added field participates in placement identity; empty fields
mean genuinely absent geometry, never unavailable reader data. No raw rule or
private configuration document is serialized into any of these fields. The
cellview path is a logical OA identity, **not** an arbitrary filesystem path.
Simple identifiers use `[A-Za-z_][A-Za-z0-9_-]*`; allow_leading_digit widens the
first character for library/cell/instance/master library/cell only. View names
remain strict. All offending names are reported together. Instance names alone
also accept slash-separated hierarchy and `<i>`/`<i:j>` iteration suffixes with
nonnegative, non-zero-padded decimal indices. Canonical stored names use uppercase
percent escapes: `X0/M1<0:3>` becomes `X0%2FM1%3C0%3A3%3E`. Raw and canonical
spellings normalize identically; duplicate aliases are rejected. Empty segments,
dot traversal, backslashes, double/lowercase percent escaping and mixed executable
syntax are rejected. These strings are identifiers, never executable expressions.
Orientation is R0/R90/R180/R270/MX/MY/MXR90/MYR90. Units are authoritative `um`,
`nm` or `m`; no inferred unit, rounded coordinates or string/Spectre suffixes are
accepted for geometry. Every coordinate (including bbox, pins, labels and boundary)
is converted using Decimal to **integer nanometres**, with stored `units=nm`.
Coordinates must be on the configured grid within 1e-6 nm float-noise tolerance;
no out-of-tolerance snapping is permitted. The same physical um/nm export hashes
identically. Sub-nm grid settings are positive and legal, but coordinates on
fractional-nm grid sites cannot be represented by this integer-nm schema and are
explicitly rejected rather than silently rounded. Existing parameter validators
check finite values, then geometry checks enforce types, grid, orientation, bbox
ordering and that origin lies within bbox expanded by bbox_tolerance_nm (errors
identify the instance). Manufacturing rules remain private and user-configured.
Instances are sorted by name; booleans, NaN, duplicate names, wrong target and
unknown fields/operations are rejected. Empty instances means an empty cellview,
not a parser failure; the adapter must distinguish those outcomes.

Two digests have deliberately different meanings:

- `sha256`: exact exported JSON bytes including the export timestamp.
- `placement_sha256`: canonical sorted JSON content **excluding** the timestamp,
  using the validated integer-nm form and all reported placement identity fields.

Thus repeated exports preserve placement identity but not timestamped artifact
identity. This is placement provenance, **not a full cellview geometry/connectivity
hash** suitable for DRC/LVS. L3 must add that separately.

`floorplan-freeze` requires a review reason, verifies the exported byte hash and
captures the live placement again. A live change publishes a new draft and stays
pending; the CLI prints FLOORPLAN_PENDING and exits non-zero, not success.
Success saves an immutable receipt under `floorplan_freezes/`, embedding
the reviewed floorplan, both hashes, input hash and review reason; task.json pins
its path and byte hash. Re-freezing after a change creates a new receipt and
preserves the previous evidence. Manual mode never creates/moves instances.

`require_unchanged_floorplan(run, checkpoint, session=held)` / `layout floorplan-check RUN
--before routing|verification` re-export and compare with the active freeze.
Any placement change clears the active approval, returns to FLOORPLAN_PENDING
and blocks the caller. Routing requires FLOORPLAN_FROZEN; verification requires
ROUTED/DRC_DONE/LVS_DONE. Public commands acquire their own lock. L3 workers must
hold **one** `with layout_lock(run) as held:` across check, use and postcondition
check, passing `session=held` to both checks without re-acquiring the lock. There
is no re-entrant/global lock. Session is invalid after exit. A second public check
under an existing lock fails instead of silently re-entering. L1's run lock is not a Virtuoso GUI
lock: concurrent human editing remains a snapshot/use race, not solved by hashes.

## Bridge and LLM trust boundaries

The only L1 operation ID is `read_placement`. The trusted adapter signature is
`read_placement(context) -> floorplan dict`. Context contains only operation ID,
exact target library/cell/view, and numeric timeout; it contains no live client,
raw SKILL, shell command, rule content or writable floorplan operation. The
worker supplies/overwrites the export timestamp and validates the full schema.
Adapters use ValueError for known input rejection and NotImplementedError for
unsupported read capability. RuntimeError/abnormal exit means unknown Bridge
state. The example stub lists all schema-2 fields and never fabricates them.
The parent supplies a run-confined `reader_results/<uuid>.json` path. The child
creates it exclusively with mode 0600 and fsyncs its JSON envelope; stdout is
not a data channel. Logs/tracebacks go only to an exclusively created mode-0600
`private_logs/reader-<timestamp>-<uuid>.log`; errors carry the relative log path,
never its content. Both trees must remain ignored by Git. POSIX permissions are
enforced by file mode; Windows deployments must restrict the user directory ACL.
Python 3.11+ children use `-P`; all versions run in a dedicated empty temporary
cwd (cleaned after exit). PYTHONPATH is pinned to the already imported ADO package
root, not inherited from a project, preventing an `analog_agent/` shadow package.

All plugins pass through `plugins.load_function`, including its module allowlist
and forbidden callable checks. Project-local Python is still trusted executable
code, **not an OS sandbox**. The operation allowlist constrains ADO dispatch and
returned JSON; it cannot prove arbitrary private Python is read-only. Audit and
pin adapter code and enforce Bridge credentials/socket permissions at deployment.
An adapter must use reviewed fixed typed templates, never expose arbitrary
SKILL, `system()`, `sh()` or generic execution/GUI recovery hooks. If it cannot
return complete precision and authoritative units, it must fail, not fall back
to the installed Bridge's permissive or rounded reader.

Readers run in a separate process with the configured timeout. POSIX readers
start a new session and timeout kills the process group; Windows uses an argv-only
PID-specific `taskkill /T /F` with direct-process fallback. This does **not**
cancel a remote CIW operation or certify detached-child cleanup. Timeout enters
NEEDS_ATTENTION and explicitly requires Bridge reconciliation. Raw adapter
stderr is not copied into handoffs, since it may contain private details.
Even if the second communicate after killing times out, the error remains
TimeoutError; no claim of remote or detached-process cleanup is made.

LLM roles may only write plan/proposal JSON, never call this adapter, freeze
their own proposals, emit executable SKILL or receive foundry rules. Private
numeric technology summaries will be the sole technology prompt input in L3.
`workflow.role_commands` remains a trusted config-level execution hook, not an
enforceable sandbox; OS/process capability separation is required for that.
L1 does not add an LLM role command or invoke an external tool automatically.

## Verification limits

Tests use temporary user projects and fake readers; they cover private/external
path checks, linked schematic tampering, strict states, locks, timestamp-neutral
hashing, export/freeze review races, tiny movement, checkpoints, receipt tampering,
reader allowlisting and local reader-process timeout. They do not certify real
Virtuoso exports, tool licenses, PDK rules, routing or DRC/LVS. STOP after L2 PR.

Schema-1 exports, inputs without the analysis spec gate, and pre-grid configs
are deliberately not migrated/re-hashed in place. Preserve old runs as evidence
and create a newly reviewed linked run with the new private config and complete
adapter. Clone requires valid current inputs, copies exact reviewed freeze bytes
with source and destination hashes checked, and does not promote inherited
evidence to active approval. L2 source/hash checks and geometric warnings are
documented separately in [the constraint contract](layout-constraints.md).
