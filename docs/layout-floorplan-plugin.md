# L4 reserved floorplan interface

This is an optional JSON proposal interface, not an automatic placement engine.
Manual mode remains the default. No placement algorithm or Bridge placement
mutation is supplied. PDK rules and site/circuit mappings remain private.

## Private configuration and rules

`private/layout/config.json` may set `layout.floorplan` to
`{"mode":"manual"}` (the default), or
`{"mode":"plugin","generator":"private/layout/floorplan_generator.py:generate"}`.
Plugin mode requires a project-local `.py:function` and the user-owned file
`private/layout/floorplan_rules.json`. Manual mode rejects a generator.
Rules, if present in either mode, are validated and pinned at layout prepare;
changing rules or generator bytes requires a new linked layout run.

Rule schema is version 1 with `units="nm"`. Every array below is required,
may be empty, and is bounded to 100,000 entries. Unknown fields are rejected.
IDs within each array are distinct, strict identifiers; instance references use
L1's canonical hierarchy/iteration escapes. Coordinates are finite parameter
atoms in nm; distances are nonnegative (widths strictly positive). Validation
returns the original document unchanged, with no inferred foundry defaults.

| Field | Entry fields |
|---|---|
| rows | id, bbox_nm, instances (nonempty), orientation (L1 orientation enum) |
| regions | id, bbox_nm, instances (nonempty) |
| symmetry_axes | id, direction (x or y), coordinate_nm |
| matching_patterns | group_id, style (common-centroid, interdigitated, none) |
| spacing | id, members (nonempty), min_nm |
| guard_rings | id, members (nonempty), width_nm, spacing_nm, layers (distinct identifiers) |
| keep_outs | id, bbox_nm |
| pin_sides | pin, side (left, right, top, bottom, any) |

Each bbox has two ordered xy corners with strictly positive extents.
`targets` is required and may contain `aspect_ratio` and/or `area_nm2`, each
`{min,max}` with positive finite ordered bounds. Optional `objective_weights`
maps any subset of area, wirelength, symmetry, matching, power to nonnegative
finite numbers. No objective or optimization algorithm is implemented.

These preferences are not DRC rules. Guard rings and pin preferences do not
create geometry; bbox keep-outs do not certify foundry spacing. Only existing
L2 frozen constraint checks and later real DRC/LVS can provide their respective
evidence. See [private empty rule template](../private.example/layout/floorplan_rules.json).

## Generator contract and explicit review

`generate(context) -> floorplan dict` is loaded through `plugins.load_function`
in the existing isolated, finite-time executor. It receives schema_version,
run_id, target, inputs_sha256, the pinned schematic link, frozen constraints,
the original validated rules document, rules_sha256, plugin_sha256,
user_placement (or null), user_placement_sha256 (or null), and the optional
validated numeric tech_summary. It receives no raw DRC/LVS rule files, runsets,
credentials or site executable configuration. Rules are passed unchanged.

Return exactly the L1 schema-2 floorplan, with source="plugin" and the additional
plugin_sha256 equal to the configured file's pinned SHA-256. All geometry,
grid, target, bbox, PCell parameter hashes and identifier checks still apply.
Rows/regions/member, matching-group and pin references must exist. These
preferences are not newly enforced placement/optimization algorithms.

```sh
analog-agent layout constraints RUN
analog-agent layout constraints-freeze RUN --reason "reviewed constraints"
analog-agent layout floorplan-generate RUN --placement private/layout/placement.json
# Inspect proposal JSON; the user separately places the live cell in Virtuoso.
analog-agent layout floorplan-freeze RUN --reason "reviewed proposal and live placement"
analog-agent layout routing-review RUN --decision proceed --reason "reviewed placement check"
```

If warnings remain, use the existing constraints acknowledgment and explicit
`proceed-with-warnings` routing decision. Generating does not change
FLOORPLAN_PENDING or authorize routing. `--placement` is optional and must be
inside the project private directory; the echo example requires it. That file
is a complete source=manual schema-2 floorplan, not a list of xy values.
Relative paths are project-relative, not shell-cwd-relative. The example
[generator](../private.example/layout/floorplan_generator.py) copies it and
changes only source/provenance; it makes no placement decisions.

Generation pins the candidate canonical document, rules, generator, linked
inputs, constraints freeze and optional user input. Files and proof receipts
remain inside the run and are checked again at freeze and later checkpoints.
Same inputs through the echo generator produce identical candidate hashes;
unique receipt filenames are audit identities, not deterministic placement.
Export timestamps and generation provenance are excluded from physical
placement identity, while full JSON and proof hashes retain them. Legacy manual
physical hashes remain unchanged. A live read must match the proposed physical
identity before freeze; on mismatch the live export is published for review,
state stays FLOORPLAN_PENDING, and the historical generation proof is retained.
After freeze the same L2 check is hash-bound into the freeze receipt, and
unchanged placement is rechecked before/after routing and verification.

## Trust boundaries and limitations

The new generate_floorplan operation is JSON-only, not a Bridge operation.
ADO dispatches no placement mutation, raw SKILL, system() or sh(). Bridge workers
continue to accept only L0's fixed typed operation allowlist. LLM roles write
plan/proposal JSON only; they do not supply executable plugins or call Bridge.
Configuration-level Python plugins and role_commands remain trusted code, not
sandboxes. A malicious plugin can perform arbitrary Python actions regardless
of its signature; owners must audit configured plugins and their dependencies.
The echo plugin is deliberately import-light and does not invoke EDA tools.
No automatic placement algorithm, live router, foundry verification or remote
deployment is claimed. Manual mode and all previous human gates remain intact.

## Read-only placement diff

`analog-agent layout floorplan-diff RUN` compares the latest generated candidate
(or manual export when there is no generation) with a fresh typed live read.
It lists canonical instance names, target origins in nm, orientations, differing
bboxes/masters/PCell parameters, missing/extra instances and pin/label/boundary
differences. It never moves anything, overwrites the candidate, freezes or
changes a healthy run's state. Empty diff exits 0; nonempty diff exits 1.
Generation retains a hash-pinned full candidate so a failed freeze's live export
does not erase the target. Older generations without that retained snapshot
must be regenerated if their candidate export was overwritten. Diff is an
advisory snapshot, not permission: freeze still performs its own live check.
