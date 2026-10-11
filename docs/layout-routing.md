# L3: JSON routing, typed execution and same-snapshot verification

This is an opt-in deterministic framework tested with fake adapters, **not a
deployed Virtuoso router or validated Pegasus/Calibre integration**. L0 found
missing terminals, net-ownership, owned-route deletion and complete snapshot
facades. Implement and audit these privately before live use. The supplied
typed_operations.py stub fails closed; there is no raw SKILL/shell fallback.
L4 and automatic placement are not implemented.

## Commands and review gates

After L2 constraints/floorplan freeze and explicit routing-review:

```sh
analog-agent layout routing-propose RUN
# Inspect the returned immutable routing_plan.json before proceeding.
analog-agent layout route RUN
analog-agent layout verify RUN
# Only following a completed failing DRC/LVS handoff:
analog-agent layout fix-propose RUN
# Inspect the new plan and affected nets before proceeding.
analog-agent layout fix RUN
```

Proposals never mutate placement, route or submit tools. route/fix are explicit
operator authorization to apply the current hash-pinned JSON proposal. verify
runs DRC then LVS and stops on failure; verification failure exits nonzero.
ROUTED is not DRC/LVS clean; unrouted nets block certification.

Explicitly applying a validated fix permits DRC_DONE/LVS_DONE -> FLOORPLAN_FROZEN
-> ROUTED -> DRC_DONE -> LVS_DONE, preserving every old receipt. Only listed
affected nets and their exact prior owned route/via IDs may be replaced. Unmapped
violations and unlisted symmetric partners need manual intervention. There is no
broad delete, instance create/move or floorplan change. max_fix_iterations defaults
to 3; exhaustion enters NEEDS_ATTENTION with the last report. Each new plan needs
explicit execution authorization; there is no unreviewed automatic fix submission.

One lock covers check/use/postcondition and, for fix, re-verification. ADO locks
do not exclude GUI editing; adapters must serialize Bridge access and export
immutable saved snapshots. Timeout/unknown execution/evidence failure requires
reconciliation, not retry. Placement changes return FLOORPLAN_PENDING and
invalidate review. DRC/LVS findings stay DRC_DONE/LVS_DONE until an explicit fix.

## Private configuration and trust

Optional additions to private/layout/config.json:

* bridge.operations: typed dispatcher function reference.
* verification_adapter: deterministic verification dispatcher reference.
* max_fix_iterations: nonnegative integer (default 3).
* tech_summary: layers map of identifier -> min_width_nm/min_spacing_nm and
  vias map of identifier -> two declared layers/min_enclosure_nm. Numbers only;
  no PDK rule code, expressions, paths or arbitrary templates.

Adapters are loaded only through plugins.load_function; project-local files and
exact private config bytes are hashed at prepare. Changed private inputs require
a new linked run. Missing adapters reject execution, not fallback.

workflow.role_commands.routing is a trusted argv array with {context}/{output}
substitutions, bounded by workflow.role_timeout_s. Timeout kills the process group;
stdout is ignored. The planner sees only frozen placement/constraints, linked
netlist, nets, numeric tech summary and hashes, plus listed violations for fixes.
It cannot submit SKILL or choose operation dispatchers via a JSON plan.

Private Python and role_commands are trusted executable configuration, **not
sandboxes**. Withholding VB_/SSH_ environment variables is defense in depth only.
For enforceable LLM isolation withhold Bridge credentials, sockets, home files,
executables and private config through OS/container permissions; protect frozen
inputs/adapter code against planner writes. Planner processes contractually write
only proposal JSON; core rejects unknown fields before execution.

## Routing plan schema, version 1

Exact top-level fields: schema_version, inputs (copy exact context bindings),
net_order (each allowed net exactly once), routes, symmetric_pairs (exact frozen
net pairs), violation_ids (exact pinned fix list). Each route has id/net/layer,
width_nm/spacing_nm/shielding (declared net or null), guide_points and vias.
Guide points are bounded integer-nm, on-grid nonzero Manhattan segments. Each via
has configured via, xy at an explicit guide point and enumerated orientation.
Unknown fields/operations, illegal nets/layers/vias, minimum width/spacing,
frozen net-class constraints, keep-out overlap and via counts are checked.
Symmetric path/layer/width/via geometry must mirror about one unambiguous frozen
axis; ambiguous or missing axes fail closed. Via physical orientation and landing
remain audited-adapter obligations.

This is explicit guide geometry, not an obstacle-solving autorouter. Numeric
screening cannot prove foundry legality, terminal contact, via enclosure or shield
continuity. Adapters must implement and read back the requested semantics. A label
is not a pin. DRC/LVS does not establish matching/symmetry by itself.

## Audited typed dispatcher contract

dispatch(request) -> JSON dict; request has operation, exact target library/cell/view,
timeout_s and data-only payload. Separate Python process, -P where supported,
pinned package path and empty cwd. Exclusive mode-0600 result files and private
stderr are retained under execution/; stdout is never the result channel.
No arbitrary SKILL, system(), sh(), shell, library mutation or recovery hook.
Only separately audited fixed wrappers over the L0 operation set:

| Operation | Payload and required return |
|---|---|
| read_layout_snapshot | Empty; full saved snapshot below |
| append_path | route_id, net, layer, width_nm, spacing_nm, shielding, points_nm; exact route_id/net/created=true |
| append_via | route_id, net, via, xy, orientation; same creation receipt |
| remove_owned_routes | previous_result ref/hash, exact route_ids, expected_cellview_sha256; exact removed_route_ids |
| save_target | Empty; saved=true and cellview_sha256 matching independent readback |
| read_terminals | expected_cellview_sha256; cellview_sha256/routed_nets/unrouted_nets/warnings covering every schematic net |
| export_verification_snapshot | saved cellview_sha256, exclusive run-relative destination; same hash and files map relative path -> actual-byte SHA-256 |

Snapshot exact fields: schema_version=1, target, saved=true, placement_sha256,
geometry, vias, nets, pins, hierarchy, params (complete master/PCell closure),
technology_sha256, declared_inputs_sha256, protected_sha256, sha256. Hash the
canonical whole object excluding only sha256 with layout_constraints.digest.
Canonical order/units and complete technology/input closure are adapter obligations;
timestamps and lock metadata are not content. protected_sha256 covers all non-worker
geometry/instances/pins/labels/boundary; it and technology/input hashes cannot
change through routing. Empty/partial snapshots cannot certify real layouts merely
by returning all required keys. Routed connectivity must be independently read.

## Verification and parser contract

verify is a separate deterministic tool capability, not a routing primitive.
Worker invokes the private verification adapter with phase=inputs, stage/tool and
private stage config; it returns ALL actually consumed transitive used_files and
tool version. Worker streams rule/runset bytes into SHA-256 before execution.
phase=run additionally gets exclusive work_dir, run-relative output_dir, immutable
export manifest, schematic run/hash link and rule hash manifest. Return exact
tool/version/exit_code=0/cellview_sha256/used_files/report/raw_reports.

Adapter launches the configured Pegasus/Calibre with fixed argv and shell=False,
process-group timeout/cancellation and explicit completion checks. License/tool
failure is not a clean report. Native tool grammars are adapted privately;
public code does not guess proprietary flags/formats. Preserve all native raw
reports and a normalized JSON report inside the exclusive run output_dir.
report is relative; raw_reports maps native AND normalized relative paths to
SHA-256. Rule bytes are rehashed after execution and before final certification.

Normalized report exact fields: schema_version=2, stage, tool, completed,
counts_by_rule, violations, lvs_match, success_marker, diagnostics. Private
verification_success_markers maps drc/lvs to audited exact single-line native
completion texts. success_marker contains found, report, line (one-based), text;
the worker verifies it against a separate hashed native report and the allowlist.
diagnostics contains nonnegative extraction_errors, soft_connection_errors,
property_mismatches, unknown_lines and boolean truncated. Missing completion,
unknown lines or truncation produce UNCERTAIN; any diagnostic error produces
FAIL. Only explicit completion and zero errors/counts produce CLEAN. Legacy v1
reports cannot certify clean and must be regenerated. These are adapter trust
contracts; actual native parsers and sanitized native fixtures remain deferred.
A violation also requires repair={class, reason}; class is routing, placement,
manual or unknown, assigned by the audited private parser, not by rule-name
heuristics or an LLM. Any placement violation (including mixed reports) returns
to FLOORPLAN_PENDING, invalidates active floorplan/routing gates and preserves
historical evidence without moving instances or spending a fix attempt.
Uncertain reports, diagnostic errors, LVS mismatch and unknown/manual or
unmapped violations enter NEEDS_ATTENTION. Only explicitly routing-fixable,
mapped violations enter the bounded fix loop. Placement required during a fix
still returns to FLOORPLAN_PENDING even when that attempt reaches the limit.
A violation has unique id, rule, layer,
ordered integer-nm bbox, message, mapped schematic nets. Counts must match the
complete list; truncation/unparsed data cannot certify clean. DRC requires
lvs_match=null; LVS requires explicit boolean match. Worker derives clean.
Fixture parsers test this normalization, not real proprietary native grammar.

drc_report.json/lvs_report.json are convenience copies. Immutable reports pin
tool/version/rule hashes/full cellview and placement hashes/counts/violations,
derived clean, routing-result ref, immutable export and native report byte hashes.
LAYOUT_VERIFIED requires both clean reports for the SAME full saved cellview and
export, current route result and unchanged placement, plus unchanged rules/raw
reports and final live readback. Never approximate full hash with placement SHA.

External work is exclusively below configured_run_dir/layout_run_id, created with
exist_ok=False and a fsynced owner marker/hash-pinned local ownership receipt.
DRC/LVS sharing a base reuse only this verified owned root; each attempt has a
fresh exclusive child. Never write in rule dirs or claim an unowned existing root.
Never delete an entire configured run base. Preserve native reports locally before
site cleanup; adapters own any separately authorized remote inventory/absence
verification. Core performs no remote shell operations or implicit cleanup.

## Release scope

### LVS source proof (required)

LVS may not compare a fresh export of the current GUI schematic. Configure
`layout.lvs_source` with `generator`, `normalizer`, explicit `top`, and
`dependencies` (project-local mapping files). All plugins and dependencies are
SHA-256 pinned at layout prepare; changing them needs a new linked run.
The example lvs_source.py deliberately fails until a complete private mapping
is supplied. Existing layout users must add this configuration before LVS.

`generate_lvs_source` gives the generator an exact local copy of the pinned
simulation design bytes, input SHA-256, explicit top and exclusive source.cdl
destination. Return relative path, actual byte SHA-256, input_sha256 and
generator_version. Generator implementation must be deterministic; no current
schematic export or uncontrolled dependency lookup. `normalize_lvs_source`
independently parses the simulation copy and generated CDL with format/path/hash
and top; each returns schema_version=1, top, ordered ports, devices, complete=true,
unparsed=[], input_sha256. Device fields: id/model/terminals (terminal->net)/
parameters (name->{type:number|enum,value}). Flattening, model aliases, terminal
order, hierarchy scope, resolved expressions and any TB exclusion must be explicit,
audited private mapping, not guessed by the public detector. No skipped constructs.
Core validates and compares complete canonical records, including case-sensitive
numeric scaling, and pins the proof plus both raw artifacts. This is not the
advisory L2 topology detector, which may warn and skip unsupported devices.

The verification adapter receives this proof and must consume that exact CDL,
return its source_cdl_sha256, and record the actual native command/source evidence
in retained reports. No proof/mismatch means no LVS submission. LVS report and
final certification bind and recheck the proof/artifact hashes. Private parsers
remain trusted audited code; equality of fabricated or incomplete parser records
is not a deployment qualification. Source mapping and native consumption need
real-tool validation, deferred in this follow-up.

Fake tests cover schema injection, grid/limits, owned operations, snapshot/placement
mismatch, DRC/LVS clean and failures, rule-byte mutation, external directory
collision and bounded reviewed fixes. They do not certify real Bridge adapters,
transport, foundry verification, native parsers or tool licensing. Validate those
separately with captured evidence before live deployment.
