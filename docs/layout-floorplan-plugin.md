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
