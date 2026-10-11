# L2 deterministic constraints

Constraint files are private JSON data, never SKILL, shell or instructions.
The user file is `<project>/private/layout/constraints.json`. Its schema is
`{"schema_version":1,"constraints":[]}`. Every item has a unique simple `id`,
`kind`, `source` (`rule`, `user`, `memory:ID`) and nonempty `rationale`.
Geometry is finite JSON numbers in nm, not simulator strings. Instance names
use the L1 canonical hierarchy/iteration spelling.

| kind | additional required fields |
|---|---|
| symmetry | `pairs` (instance pairs), `axis` (`direction`: x/y, `coordinate_nm`), `tolerance_nm` |
| matching | `groups` (arrays of distinct instances), `style`: common-centroid/interdigitated/none, `topology`: differential_pair/current_mirror/cascode/resistor/capacitor/user, `tolerance_nm` |
| net_class | `nets`, `class`: critical/sensitive/power/ground, positive `width_nm`, nonnegative `spacing_nm`, `shielding` (net or null), `layers` (preference order) |
| keep_out | ordered `bbox_nm`: [[xmin,ymin],[xmax,ymax]] |
| symmetric_routing | `pairs` (net pairs) |
| max_vias | `nets`, nonnegative integer `max_count` |

## Retrieval and conservative detection

The linked, verified netlist is read and checked against its frozen SHA-256.
The parser supports parenthesized Spectre instances and `subckt NAME (PORTS)`;
unparameterized hierarchy expands with slash instance and local-net names.
Recursive hierarchy, bad terminal counts and duplicate names are errors.
Includes are **not** opened, expressions are **not** evaluated, parameterized
subcircuits and unknown syntax/models produce explicit warnings, not guessed
devices. This is not a complete Spectre elaborator.

Private `layout.detection` optionally supplies `top` and `models`: each model
maps to `{kind, terminals}` with explicit ordered terminals. Kinds mos/bjt
require d/g/s or c/b/e respectively (additional body terminals are permitted);
resistor/capacitor require p/n. Only the simulator primitives resistor and
capacitor have built-in mappings. There is no foundry-model name inference.
Equal model and parameter tokens are compared conservatively: shared source
with different control/output suggests a differential pair; shared control
and source with one diode connection suggests a mirror; series output/source
with distinct controls suggests a cascode; equal passive tokens suggest matching.
These are candidates, not electrical proofs. Detections propose matching style
`none`, not an invented symmetry axis or an automatic common-centroid pattern.

Existing `memory.retrieve` supplies only compatible **reviewed** entries under
its configured item and serialized-character caps. Only `kind=layout_constraints`
is interpreted, and only when its bounded summary is itself a schema-1 constraint
JSON object. Free text is never executed or converted to constraints. Larger
constraint sets belong in the user file, not an uncapped full-entry retrieval.
Candidate/rejected memory remains excluded. Malformed structured summaries
produce warnings. Source IDs remain attached. No new memory write-back occurs.

Merge priority for the same stable constraint ID is rule < memory < user.
User items replace whole records and are always tagged `user`; use the detected
ID to override detection. Stable IDs and sorting make draft bytes deterministic.
The draft records netlist, derived hierarchy, user-file, layout-input and memory
selection/source hashes. Rule and private configuration contents are never
copied into public repository files or prompts.

Drafts include `coverage` counters (mapped/unmapped devices, expanded/skipped
hierarchy, unsupported statements and skipped instances) and `overrides` with
the complete before/after records and changed fields. Coverage is limited to
the parsed/expanded subset; skipped hierarchy is not claimed to contain zero
devices. Every rejected selected reviewed-memory summary produces a warning
with its ID and JSON location/reason or schema/reference failure. Retrieval
caps omit entire entries, not truncate their summaries; malformed or externally
truncated JSON is never silently accepted or recovered.

The `det_` ID prefix is reserved for current rule detections. User constraints
using that prefix must target an extant detection; independent user IDs remain
valid. All user instance/net/shield references must resolve in the observed
linked-netlist hierarchy, including known unmapped instances; unexpanded or
unsupported references fail closed. An orphan user reference blocks drafting,
not a warning that can be waived. Memory reference failures instead identify
the rejected entry in warnings. No absent device is silently removed from a group.

## Human review gate

`analog-agent layout constraints RUN` publishes deterministic
`layout_constraints.draft.json`, records its SHA-256 and enters
CONSTRAINTS_DRAFTED. It may be regenerated only while still at this gate.
`analog-agent layout constraints-freeze RUN --reason TEXT` rechecks the pinned
draft and all sources, writes an immutable hashed receipt under
`constraint_freezes/`, records CONSTRAINTS_FROZEN and then FLOORPLAN_PENDING.
The explicit command is the human approval, not an automatic worker decision.
If the draft has unresolved warnings, `--ack-warnings` is additionally required;
a reason alone is insufficient. The receipt pins the exact warning-set and
coverage hashes and records the acknowledgment. It never waives orphan errors.
Editing the draft directly is tampering; revise the private user file and
regenerate before approving. A changed user/memory source refuses stale review.
Tampered pinned evidence enters NEEDS_ATTENTION. Input/schema errors leave
the current state unchanged. After freezing, changes require a new linked run;
drafts and inherited evidence never grant active approval. Later consumers
must verify the frozen receipt and source hashes under one layout lock and
include its SHA-256 in their own handoff. No command runs routing or DRC/LVS.

## Manual placement checks

After a successful manual `floorplan-freeze`, a check runs under the same held
lock. `analog-agent layout constraints-check RUN` repeats it without calling
Bridge or moving instances. An immutable schema-2 `placement_checks/<uuid>.json`
snapshot pins placement identity, input and constraint receipt hashes and the
warning-set hash. The floorplan-freeze receipt embeds that snapshot's path and
SHA-256. A repeated check recomputes and verifies it rather than replacing frozen
evidence. `floorplan_check.json` is only a convenient readable copy. The report
does not include its containing freeze receipt's hash, avoiding a circular hash
dependency; the freeze receipt and human routing decision pin the full chain.
Checks use canonical integer-nm bboxes:

- symmetry reflects bbox centers about the declared x/y axis, comparing both
  centers and extents within tolerance; device orientation/internal geometry
  is not proven by this check;
- common-centroid requires at least two explicit groups of equal-master,
  equal-parameter, equal-extent units, equal group centroids and a centrally
  symmetric unit-center pattern in each group;
- interdigitation checks alternation along a single horizontal row;
- keep-outs warn on bbox/interior overlap; touching a boundary is allowed by
  this geometry check only, not proof of minimum spacing or a substitute for DRC;
- missing user-referenced instances are hard errors before live freeze capture;
  missing rule/memory members, unequal matching units and coincident centers
  produce warnings, never automatic repair instructions.

These are advisory necessary geometric checks, not a complete matching proof.
Equal unit weighting is assumed; multiplicity must be expanded into individual
placement records. Higher-order gradient cancellation, dummy strategy,
orientation, routing/shielding/width/spacing/via counts and foundry verification
remain unverified (L3 or a future checker). Style `none` creates no pattern
claim. Warnings do not prevent a human freeze or silently alter placement.

The floorplan freeze embeds the active constraint receipt reference. Both live
placement checkpoints and frozen checks revalidate constraint sources before
use. Changed/tampered frozen evidence enters NEEDS_ATTENTION; a live placement
change still follows the L1 FLOORPLAN_PENDING rule. Future L3 workers must
carry both hashes and hold one lock across checks, use and postconditions.
Legacy L1-only freezes without the constraint link are refused, not migrated
or silently approved; prepare/clone a new run and pass both human gates.

## Explicit routing release (L2 gate only)

`analog-agent layout routing-review RUN --decision proceed --reason TEXT`
records a human decision after a clean check. If any placement warnings remain,
the decision must be `proceed-with-warnings`; `proceed` or a reason alone is
rejected. This command does not route, submit a job or advance to ROUTED.
It rechecks frozen inputs and live placement under one lock and saves an
immutable review receipt with the floorplan/constraint/check references and
the exact warning-set hash. All routing checkpoints require this decision,
even when clean; draft-warning acknowledgment is not routing authorization.
No LLM worker may self-approve this gate. Future L3 workers must call the
lock-held checkpoint before using the evidence and propagate the review hash.
Movement or re-freeze clears the active decision; old files remain evidence.
Tampering blocks release. Checks/freeze receipts from before this hash-chain
and warning-confirmation contract are not silently migrated or auto-approved.

Future matching metrics may quantify group second moments/dispersion and
assumed spatial-gradient sensitivity, with units, weighting and provenance.
Those numerical measures are intentionally not implemented by this L2 fix and
would not by themselves establish electrical matching or foundry compliance.
