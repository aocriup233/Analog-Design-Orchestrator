# Layout L1: configuration, linked states and manual floorplan

L1 is an opt-in framework with fake-backend tests, not a certified live Virtuoso
workflow. L2 constraints, L3 routing/DRC/LVS and L4 generator are not implemented.
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
analog-agent layout floorplan-export LAYOUT_RUN_DIR
# After L2 has frozen constraints and reached FLOORPLAN_PENDING:
analog-agent layout floorplan-freeze LAYOUT_RUN_DIR --reason "reviewed placement"
```

Layout runs live in `<project>/layouts/<id>/`, separately from schematic `runs/`.
Ignore `layouts/` in independent project Git repositories. Preparation requires
a VERIFIED or ANALYZED schematic run and a matching READY netlist/DONE simulation
receipt with no errors. It never changes the schematic's files or status.
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
L1 has no command to fabricate constraint approval, routing or clean DRC/LVS.
Exports before FLOORPLAN_PENDING are draft snapshots only. Freeze refuses them
until L2 provides the constraints human gate. Fake tests advance fixture states
explicitly; production CLI does not bypass missing stages.

Mutating layout commands use an exclusive run lock. A stale `.layout.lock` is
never silently reclaimed: inspect the owning process and reconcile any Bridge
operation before removing that exact lock. Evidence/config/schema/reader errors
enter NEEDS_ATTENTION with sanitized diagnostics; invalid command ordering is
rejected without advancing state. `layout status` revalidates inputs; inspect
`task.json` directly if changed inputs prevent validation.

## Floorplan schema and hashing

`floorplan.json` has exactly:

```json
{
  "schema_version": 1,
  "source": "manual",
  "cellview": {"library": "USER_LIBRARY", "cell": "USER_CELL", "view": "layout", "path": "USER_LIBRARY/USER_CELL/layout"},
  "units": "um",
  "export_timestamp": "UTC ISO-8601 timestamp assigned by worker",
  "instances": []
}
```

Each instance contains exactly `instance` (unique identifier), `master` with
`library`, `cell`, `view`, finite JSON numeric `x`, `y`, `orientation`, and
`bbox: [[xmin, ymin], [xmax, ymax]]` in the top-level coordinate frame. The
cellview path is a logical OA identity, **not** an arbitrary filesystem path.
Simple identifiers use `[A-Za-z_][A-Za-z0-9_-]*`; array/hierarchical names outside
this conservative subset are currently rejected, not silently renamed.
Orientation is R0/R90/R180/R270/MX/MY/MXR90/MYR90. Units are authoritative `um`,
`nm` or `m`; no inferred unit, rounded coordinates or string/Spectre suffixes are
accepted for geometry. Existing parameter validators check finite numeric values,
then geometry-specific checks enforce types, orientation and bbox ordering.
Instances are sorted by name; booleans, NaN, duplicate names, wrong target and
unknown fields/operations are rejected. Empty instances means an empty cellview,
not a parser failure; the adapter must distinguish those outcomes.

Two digests have deliberately different meanings:

- `sha256`: exact exported JSON bytes including the export timestamp.
- `placement_sha256`: canonical sorted JSON content **excluding** the timestamp,
  with int/float geometry normalized and negative zero normalized to zero.

Thus repeated exports preserve placement identity but not timestamped artifact
identity. This is placement provenance, **not a full cellview geometry/connectivity
hash** suitable for DRC/LVS. L3 must add that separately.

`floorplan-freeze` requires a review reason, verifies the exported byte hash and
captures the live placement again. A live change publishes a new draft and stays
pending. Success saves an immutable receipt under `floorplan_freezes/`, embedding
the reviewed floorplan, both hashes, input hash and review reason; task.json pins
its path and byte hash. Re-freezing after a change creates a new receipt and
preserves the previous evidence. Manual mode never creates/moves instances.

`require_unchanged_floorplan(run, checkpoint)` / `layout floorplan-check RUN
--before routing|verification` re-export and compare with the active freeze.
Any placement change clears the active approval, returns to FLOORPLAN_PENDING
and blocks the caller. Routing requires FLOORPLAN_FROZEN; verification requires
ROUTED/DRC_DONE/LVS_DONE. Future workers must call this immediately before routing
and again before verification, holding their execution/session lock across
checking and use, and check postconditions. L1's run lock is not a Virtuoso GUI
lock: concurrent human editing remains a snapshot/use race, not solved by hashes.

## Bridge and LLM trust boundaries

The only L1 operation ID is `read_placement`. The trusted adapter signature is
`read_placement(context) -> floorplan dict`. Context contains only operation ID,
exact target library/cell/view, and numeric timeout; it contains no live client,
raw SKILL, shell command, rule content or writable floorplan operation. The
worker supplies/overwrites the export timestamp and validates the full schema.
Adapter logging is separated from the JSON stdout channel.

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
Virtuoso exports, tool licenses, PDK rules, routing or DRC/LVS. STOP after L1 PR.
