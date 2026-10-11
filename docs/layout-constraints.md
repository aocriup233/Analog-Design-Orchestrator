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

## Human review gate

`analog-agent layout constraints RUN` publishes deterministic
`layout_constraints.draft.json`, records its SHA-256 and enters
CONSTRAINTS_DRAFTED. It may be regenerated only while still at this gate.
`analog-agent layout constraints-freeze RUN --reason TEXT` rechecks the pinned
draft and all sources, writes an immutable hashed receipt under
`constraint_freezes/`, records CONSTRAINTS_FROZEN and then FLOORPLAN_PENDING.
The explicit command is the human approval, not an automatic worker decision.
Editing the draft directly is tampering; revise the private user file and
regenerate before approving. A changed user/memory source refuses stale review.
Tampered pinned evidence enters NEEDS_ATTENTION. Input/schema errors leave
the current state unchanged. After freezing, changes require a new linked run;
drafts and inherited evidence never grant active approval. Later consumers
must verify the frozen receipt and source hashes under one layout lock and
include its SHA-256 in their own handoff. No command runs routing or DRC/LVS.
