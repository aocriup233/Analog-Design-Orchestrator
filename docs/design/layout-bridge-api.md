# Layout Bridge API audit and proposed typed boundary (L0)

Status: inspection/design only, 2026-10-11. No layout role, state, CLI, parser,
plugin or runtime implementation is introduced by L0. L1-L4 require separate
reviewed stages. Existing users see no behavior change.

## Inspected installation and scope

The installed distribution metadata reports `virtuoso-bridge 0.8.0`, Python
`>=3.9`, editable installation. Its installed source files were read without
importing the package or invoking CIW. SHA-256 comparison confirmed the following
files are byte-identical to the locally inspected source. Package version alone
does not pin an editable install; record these hashes again at each deployment.
No remote Git commit identity was established. No PDK, rule content, site path,
credentials or circuit-specific example is included here.

Paths in this document are relative to the Bridge Python package, not ADO.

| Inspected file | Installed/local SHA-256 |
|---|---|
| `virtuoso/layout/__init__.py` | `5bdcd03b3abd0790f92e80a6285e6210e21c0ce453e6abfac0aa6ee93738789a` |
| `virtuoso/layout/editor.py` | `9e36ad04843b71e3636f2979b86a301352397df9f8830f544431615596c16a3f` |
| `virtuoso/layout/ops.py` | `fedbd6841ad95b5843d1dd505d76679b3a09996c5596e1fff71105024a15a60e` |
| `virtuoso/layout/reader.py` | `1776a873b6a070d247784ad1f0df1726593298ff2726ae2a5c152b4bdf1741c2` |
| `virtuoso/layout/xstream.py` | `c869ac7b099d3cc46247c55201a0e56dae75e88eaa225cb9eeeb56147f8ebf65` |
| `virtuoso/layout/streamout.py` | `4a40e1ea23514e9a5c8eac61f9945a7972264239d0ec7e5552c1f2cdba697a8a` |
| `virtuoso/basic/bridge.py` | `cc6c2022814c8102ba1ecb04171a866b62c6b9fad84cae75f8eced0f255d89e3` |
| `virtuoso/ops.py` | `8b5a5861638a51ef3d2530b0913f2ad19a220de58d73583ac4d049580e375523` |
| `virtuoso/editor.py` | `da0a97b921b96cc5ab94e834fc6ad99f16acdf919e9f62fe0c2b1ef9eaf59a94` |
| `virtuoso/basic/composition.py` | `0c7214f2eafbf0fe473b4cea0534ba95e1f8414381a3a0817e330841890ae341` |
| `transport/ssh.py` | `283f87873d3f9abf39891943a56bca8955968bab4fcf2d4d2f838950636ef8ed` |
| `transport/paramiko_backend.py` | `205ebd1da52d6a752758c7c564b1e1498f126e9a4d714aee08d20f1e1ae1b9cc` |

Inventory scope: every public callable exported by `virtuoso.layout`, the
layout-relevant shared cellview builders and client execution/transfer surfaces.
Library context and X11 recovery are recorded separately. Schematic, symbol and
Maestro APIs are not layout APIs; their arbitrary execution channel is the same
client channel audited below. Underscore-only implementation helpers are not an
ADO operation interface. Source search covered the package for auto/guided
routing, placement export, stream import and layout pin/net functions; examples
were checked separately and are not promoted to installed APIs.

## Common execution and error contract

Most `layout_*` functions are **pure string builders**: calling them does not
edit Virtuoso. They return SKILL text, have no execution timeout, and generally
do not validate names, finite geometry, legal layers, or physical constraints.
Formatting/type errors can raise in Python. String escaping protects string
literals, not the raw expression arguments discussed below.

When that text is executed through `VirtuosoClient.execute_skill`, the client
default timeout is 30 seconds (`__init__(host='127.0.0.1', port=65432,
timeout=30, tunnel=None, log_to_ciw=True)`). `None` uses the configured client
timeout. The transport deadline bounds connect/send/receive, not guaranteed
cancellation of an already-running SKILL mutation or child tool.
Socket timeout/refusal/OSError becomes `VirtuosoResult(status=ERROR, errors=...)`.
Empty responses and daemon timeout markers are errors; unmarked responses can
be SUCCESS with a warning. A `nil` or text `ERROR: ...` return is not necessarily
an execution-status error. ADO must validate response shape, status, errors and
postconditions, never just truthiness or the lack of a Python exception.

The raw SKILL channel can run `system()`, `sh()` or other arbitrary code; no
operation allowlist exists in Bridge itself. A timeout after sending a mutation
is indeterminate: reconcile by readback, do not blindly repeat it. Batch edits
are not database transactions and do not roll back partial changes.

## Cellview and edit APIs

Signatures below omit `self`; Python annotations are summarized without changing
arguments/defaults. `str` builders inherit the no-execution contract above.

| API and signature | Side effects / return / error and timeout |
|---|---|
| `client.open_cell_view(lib, cell, *, view=None, view_type=None, mode='a', timeout=None)` | Executes fixed open builder, binds global `cv`; defaults to layout/maskLayout. Modes can create/modify/replace. Returns VirtuosoResult; shared timeout/error contract. |
| `client.open_window(lib, cell, *, view='schematic', view_type=None, timeout=None)` | Executes fixed GUI builder; raises/reuses matching window, else opens append-mode GUI. Must explicitly select layout. Shared result/timeout. |
| `client.save_current_cellview(timeout=None)` | Saves bound `cv` if present, otherwise current GUI cellview; stale/global target hazard. Shared result/timeout; nil need not be ERROR. |
| `client.close_current_cellview(timeout=None)` | Closes bound/current database cellview; same target hazard and result/timeout. |
| `LayoutOps.create(lib, cell, view='layout', timeout=60)` | Returns LayoutEditor in mode `w`, explicitly replacing existing view; execution occurs at context exit. Not safe for automatic reuse of user layout. |
| `LayoutOps.modify(lib, cell, view='layout', timeout=60)` | Returns append-mode LayoutEditor, may reuse matching edited cellview. |
| `LayoutOps.edit(lib, cell, view='layout', mode='a', timeout=60)` | Deprecated compatibility wrapper; emits DeprecationWarning, constructs editor with supplied mode. |
| `LayoutEditor(client, lib, cell, view='layout', mode='a', timeout=60)` | `__enter__()` queues open/bind only; `add(skill_cmd: str)` queues **arbitrary SKILL**; `close()` queues close. On successful Python context exit, queues save and executes batch with editor timeout. Failed execution raises RuntimeError via ensure_operation_response. Python context exception means no queued execution. A close before appended save can affect postconditions. |
| `ops.open_cell_view(lib, cell, *, view='layout', view_type=None, mode='a') -> str` | Fixed `dbOpenCellViewByType` builder; binds `cv`, no missing-view postcheck. |
| `ops.open_window(lib, cell, *, view='layout', view_type=None, mode='a') -> str` | Fixed `geOpen`/raise builder. Unlike most strings, `mode` is interpolated without escape in this builder; never forward an unvalidated mode. |
| `ops.save_current_cellview()`, `ops.close_current_cellview() -> str` | Fixed save/close builders, prefer global `cv` over GUI edit view. Re-exported close in layout. |
| `layout_bind_current_or_open_cell_view(lib, cell, *, view='layout', view_type=None, mode='a') -> str` | Fixed matching-current/open builder. Current matching view may be reused even when requested mode is read-only; cannot assume a persisted-only snapshot. |

None of the fixed builders calls shell directly. A caller accepting their returned
strings as arbitrary batches still creates an arbitrary-SKILL execution surface.

## All public layout builders and reader

Abbreviations used only in the table: `C` means keyword-only `cv_expr='cv'`;
`R` means keyword-only `view='layout', view_type=None`; `W` means keyword-only
`view='layout', view_type=None, mode='a'`. `points` is an iterable of `(float,
float)` pairs, `layers` of `(str, str)` pairs, and bbox a four-float tuple.
`C` is inserted **as raw SKILL**, not quoted. These abbreviations expand to the
actual inspected signatures, not proposed new arguments. Every table builder
returns `str`, has no timeout, executes neither SKILL nor shell until dispatched.

| Signature | Effect when executed / special failure or trust concern |
|---|---|
| `layout_create_param_inst(lib, cell, view, instance_name, x, y, orientation, *, C)` | `dbCreateParamInstByMasterName`; places default-parameter PCell. No typed PCell parameter map, no orientation/finite-value validation. Evaluating a PCell executes trusted installed PDK code. |
| `layout_create_simple_mosaic(lib, cell, *, origin=(0.,0.), orientation='R0', rows, cols, row_pitch, col_pitch, view='layout', view_type=None, instance_name=None, cv_expr='cv')` | Opens master read-only, creates mosaic; no bounds/pitch validation or master-close guarantee. |
| `layout_create_path(layer, purpose, points, width, style=None, *, C)` | `dbCreatePath` only; no route search, obstacle avoidance, net association or connectivity checking. |
| `layout_create_rect(layer, purpose, x0, y0, x1, y1, *, C)` | `dbCreateRect`; no bbox ordering/rule validation. |
| `layout_create_polygon(layer, purpose, points, *, C)` | `dbCreatePolygon`; no legality/self-intersection validation. |
| `layout_create_label(layer, purpose, x, y, text, justification, rotation, font, height, *, C)` | `dbCreateLabel`; text label is not an electrical terminal/pin. |
| `layout_create_via(via_def_expr, x, y, orientation, via_params_expr, *, C)` | `dbCreateVia`; **both expression arguments are arbitrary SKILL**. Reject public forwarding. |
| `layout_find_via_def(via_name, *, C)` | `techGetTechFile`/`techFindViaDefByName`; returns handle or nil, no missing-definition error guard. |
| `layout_via_def_expr_from_name(via_name, *, C)` | Alias renderer for the preceding lookup, same trust/error contract. |
| `layout_create_via_by_name(via_name, x, y, orientation='R0', via_params_expr='nil', *, C)` | Fixed name lookup then via creation; raw via_params_expr remains injection-capable, nil lookup not guarded. |
| `layout_read_summary(lib, cell, *, R)` | Opens read-only and formats shape/instance summary; instance xy is rounded to 3 decimals, no full placement schema. Returns `ERROR: cannot open ...` text on failure; no dbClose. |
| `layout_read_geometry(lib, cell, *, R)` | Opens read-only, emits tab-separated shapes and instances, including name/master/view/xy/orient/transformed master bbox/transform. No units, source path, timestamp, explicit net/pin/via/parameter closure; no dbClose. |
| `parse_layout_geometry_output(raw: str) -> list[dict[str, Any]]` | Pure parser, no SKILL/shell/files. Permissive field parsing, ignores malformed fields; regex numeric conversion lacks exponent support. Not a strict or complete geometry/placement validator. |
| `layout_list_shapes(*, W)` | Reads shape types/LPP from an open edit window; GUI-dependent, may open append-mode view. Missing-window text error; no dbClose. |
| `layout_fit_view()` | Fits current GUI window, returns nil if none; no specific cell target. |
| `layout_set_active_lpp(layer, purpose='drawing')` | Changes active palette LPP through pteSetActiveLpp. |
| `layout_show_only_layers(layers)` | Hides all palette layers then shows supplied LPPs. |
| `layout_show_layers(layers)`, `layout_hide_layers(layers)` | Changes palette visibility; empty iterable renders nil. |
| `layout_highlight_net(net_name, *, W)` | GUI finds first shape on named net and invokes net marking; missing-window/net text error. Does not create or verify connectivity. |
| `layout_select_box(bbox, *, mode_name='replace', view='layout', view_type=None, mode='a')` | GUI selection; replace/add/subtract variants. Unsupported mode_name raises ValueError at rendering; missing-window text error at execution. |
| `layout_delete_selected(*, W)` | Deletes GUI-selected objects via leHiDelete. Target depends on GUI state; does not limit deletion to worker-owned routing. |
| `layout_delete_shapes_on_layer(layer, purpose='drawing', *, W)` | Deletes ALL shapes on LPP, including pins/user figures; no automatic save. |
| `layout_clear_routing(*, W)` | Deletes ALL top-level shapes and saves; preserves instances but can erase user pins/keep-outs/nonrouting shapes. Name is misleading for a fix loop. |
| `layout_delete_cell(lib, cell)` | Saves/closes matching windows and ddDeleteObj deletes the CELL, not just layout view. Destructive; no shell directly. |
| `clear_current_layout()` (re-export from shared ops) | Makes layers visible, selects/deletes GUI figures and redraws. GUI/global targeting, not restricted to generated routes or necessarily only shapes. |

No installed layout-facing `create_pin`, `create_net`, `add_fig_to_net`,
`create_connection`, instance-move or owned-route-delete facade was found.
Symbol/schematic pin helpers exist in other modules but have different view/layer
semantics and are not interchangeable with layout pins. Native SKILL primitives
may exist; that is not evidence of a typed Bridge API. L0 does not invent their
signatures or expose them as supported layout operations.

## Placement export, routing and cellview fingerprint gaps

There is no `export_placement`/`floorplan_export` API in the inspected package.
`layout_read_geometry` plus its parser supplies only part of L1's requested
schema. A future typed read must obtain explicit units/precision, cellview
identity/path, full instance/master data and export provenance; reject incomplete
reads. Do not assume display coordinates have a universal unit or confuse master
and transformed bboxes. A consistent snapshot needs protection against editing
between queries; a persisted snapshot policy must also distinguish unsaved edits.

No auto-router or guided-routing solver was found. Paths, rectangles and vias
are drawing primitives, not a connectivity-aware router. L3 guide points would
have to be validated and rendered by a deterministic ADO worker; no existing
Bridge route method will decide geometry for it. Net ownership, via landing,
terminal contact and partial reroute remain missing typed capabilities.

No canonical cellview SHA-256 API was found. A screenshot, shape count or
instance-only placement hash cannot certify the routed cellview. An eventual
snapshot hash must cover geometry, vias, nets/pins/connections, instance/PCell
parameters, hierarchy and declared technology/input identity. Raw OA timestamps
or lock files are not a deterministic geometry fingerprint. Same-hash DRC/LVS
requires an immutable saved/exported verification snapshot and live-read checks.

L1 floorplan export has a timestamp for provenance. Store both the complete-file
hash and a canonical placement-content hash excluding timestamp; compare the
latter on re-export or unchanged placement would always appear changed. This
is a proposed schema clarification for L1 review, not code added in L0.

## Stream out and stream in

Found stream-out signatures:

```text
client.layout.export_gds(library, cell, output_path, *, stream_map,
    view='layout', log_path=None, timeout=300.0, poll_interval=0.5,
    skill_timeout=30.0, finalization_reserve=30.0,
    cleanup_policy='success', recovery_hook=None) -> GdsExportResult
layout.export_gds(client, library, cell, output_path, *, same keyword arguments)
XStreamExportRequest(library, top_cell, view, stream_file, layer_map, log_file, run_dir)
xstream_export_gds_skill(request: XStreamExportRequest) -> str
parse_xstream_log(text: str) -> XStreamLogResult
```

The request renderer is pure, validates nonempty strings and escapes values;
execution uses fixed `xstGetField`, `xstSetField`, `xstOutDoTranslate`,
`isCallable`, `errset` and `unwindProtect`. It temporarily changes XStream fields
and completion-dialog settings, then attempts restoration, returning a structured
request response. These are fixed SKILL operations, not a caller-supplied script.
They can launch the translator internally; submission does not mean completion.

The high-level export resolves caller-host paths, validates existing stream-map
file, distinct path identities, positive finite budgets, allowed cleanup policy
and reserve < timeout. Preflight raises ValueError/FileNotFoundError. Operational
staging/transport/publication errors normally return GdsExportResult rather than
raise. All controls above matter: launch uses bounded skill_timeout within a
total budget with finalization reserve, polling uses the remaining budget.
Results include status/reason/timed_out, local/remote run paths, errors/warnings,
parsed log and tri-state remote_files_retained (`False`, `True`, `None`).

Success requires current-run zero-error parsed completion and fresh nonempty GDS;
an old destination is not success evidence. No DRC or LVS is run. GDS/log have
separate publication steps, not a multi-file transaction. Cleanup may fail or
remain unknown without changing successful translation into failure; ADO must
apply its stronger cleanup/evidence policy before completing its own handoff.

Local export uses filesystem staging/polling/publication and fixed SKILL. Remote
export invokes SSHRunner.run_command for staging, observation, SHA-256 tools,
snapshot/finalization and deletion, plus upload/download. Thus remote export
**executes shell commands**, even though local subprocess calls use argument
arrays rather than shell=True. recovery_hook is an arbitrary Python callable,
potentially run once after an indeterminate launch with progress; ADO must keep
it None. Path inputs are not confined to an ADO run by Bridge. Existing validated
destination artifacts can be replaced on success; ADO needs overwrite policy.

GdsExportReason cases: completed, xstream_failure, xstream_errors,
request_cleanup_error, skill_error, launch_indeterminate, incomplete_log,
missing_gds, empty_gds, malformed_log, staging_error, transport_error,
publication_error. Request cleanup/error priority and result.ok must be checked
alongside retained-files evidence; successful translation is not signed-off layout.

No installed stream-in facade was found. The separate `digital_import/import_gds.py`
example builds a `strmin` command and calls `client.execute_skill(system(...))`,
then polls cellview/log evidence because return status is unreliable. It writes
library views and reports and can outlive the request. It is an example, not a
typed stream-in API, and is **excluded** by the requested system/sh prohibition.
No import example is automatically allowed merely because it ships with Bridge.

## Raw execution, transfer and diagnostic surfaces

All following client signatures are real. Unless noted, timeout=None uses the
client default. None is an acceptable LLM-facing callable.

| API signature | Arbitrary execution / effects / errors |
|---|---|
| `execute_skill(skill_code: str, timeout: float | None=None) -> VirtuosoResult` | Arbitrary SKILL, including shell-capable primitives. Shared deadline/results above. Reject caller-supplied use. |
| `execute_operations(commands: list[str], *, timeout=None, wrap_in_progn=True)` | Arbitrary SKILL batch. Empty batch composition yields error result; no atomicity. |
| `compose_skill_script(commands: Iterable[str], *, wrap_in_progn=True) -> str` | Pure arbitrary-code composer, rejects empty input with ValueError; not an operation allowlist. |
| `fetch(expr: str, fields: list[str], *, timeout=None) -> list[dict]` | Raw expression AND slot names interpolated as SKILL. Can execute mutations/shell; not inherently read-only. Uses result.output without explicit status check; can hide failures as empty output. |
| `fetch_one(expr, fields, *, timeout=None) -> dict` | Wraps expr in list then fetch; same arbitrary-input hazards; empty dict not proof of absence. |
| `load_il(path: str | Path, timeout=None)` | Arbitrary file code load. Optional upload/cache; preparation failures become error result. Cache uses MD5, not ADO SHA-256 contract. Remote staged IL can remain. |
| `run_il_file(path, lib, cell, *, view='layout', view_type=None, mode='a', open_window=True, save=False, timeout=None)` | Open/GUI/global-cv synchronization/load/optional save, stops on failed phase. Arbitrary IL; timeout applies per call, not a single whole-operation budget. |
| `run_shell_command(cmd: str, timeout=None)` | Executes arbitrary shell through SKILL `csh`; nil becomes explicit error result. Prohibited. |
| `ciw_log(skill_code: str, timeout=None)` | Alias for arbitrary execute_skill, despite log name. Prohibited. |
| `ciw_print(message: str, timeout=None)` | Escaped fixed printf, changes CIW display; still must restrict/redact data, format-sensitive message handling is not an audit logger. |
| `get_current_design(timeout=None) -> tuple` | Fixed SKILL query; parses path, returns three None on failures/invalid output. GUI-dependent, not authoritative target identity. |
| `list_windows(timeout=None) -> list[dict]` | Fixed SKILL query, window num/name. Returns [] on failures; can conflate no windows and failed read. |
| `screenshot(output: str | Path | None=None, *, target: str | int='ciw', timeout=None)` | Fixed capture SKILL, may run remote mkdir shell, creates remote PNG then downloads/copies. Per-step timeout, no overall transaction deadline or automatic screenshot cleanup. Naming uses second-resolution timestamps; collision possible. Returns VirtuosoResult, missing window explicit ERROR. Not a cellview fingerprint. |
| `download_file(remote_path, local_path, *, timeout=None, recursive=False)` | Transfer or local copy, can replace target; recursive local copy removes existing destination after overlap check. Remote transport may execute shell; nonzero return becomes error, transport exceptions may propagate. Local copy OSError becomes error; local copy has no interruptible timeout. |
| `upload_file(local_path, remote_path, *, timeout=None)` | Transfer/copy may replace target; missing source/error return handled, transport exceptions may propagate; local copying not deadline-controlled. |
| `SSHRunner.run_command(command: str, timeout: float | None=None) -> CommandResult` | Arbitrary remote shell. Paramiko executes `sh -l` and sends command; OpenSSH/persistent-shell paths also run remote command strings. Deadline may raise subprocess.TimeoutExpired; transport failures can return nonzero status. Exclude generic command dispatch. |

## Context and recovery APIs (not routing)

LibraryOps provides `list(*, timeout=30)`, `get(name, *, timeout=30)` and
`get_technology_library(name, *, timeout=30)`: fixed SKILL reads, structured
library info/path/technology, Python exceptions on bad inputs/failed postchecks.
Mutating context APIs are `create(name, path, *, technology_library=None,
timeout=60)`, `set_technology_library(name, technology_library, *, timeout=60)`,
`rename(name, new_name, *, timeout=120)`, `delete(name, *, timeout=60)`.
They execute fixed library SKILL, can create/change/delete durable library data,
and creation/binding partial success raises LibraryPartialSuccessError with the
created library rather than rollback. Deny these in normal layout routing.
Category management also exists but is not a layout operation and is unnecessary
for the proposed worker. Do not expose the LibraryOps object to a planner.

`client.dismiss_dialog(display=None)` uses X11 helpers/SSH shell, not SKILL,
may stage a helper and send UI keys (dangerous against unsaved user work).
X11 module signatures: `find_dialogs(runner, user, display=None, profile=None)`,
`list_windows(runner, user, display=None, profile=None, top_level=False)`,
`dismiss_window(runner, user, window_id, *, action='enter', display=None,
profile=None)`, `bootstrap_ciw(runner, user, window_id, setup_path, *,
display=None, profile=None)`, `dismiss_dialogs(runner, user, display=None,
profile=None)`. Normal helper commands use 15-second timeouts; bootstrap 20.
Helper-discovery/staging adds work, so these are not a full transaction deadline.
Parsers can return empty lists on failed/unparseable output; failures/exceptions
need separate evidence. Bootstrap types a load expression into CIW: arbitrary
setup-file execution, not a layout primitive. All recovery/bootstrapping is
operator-only; no implicit dialog acceptance or bootstrap in a routing loop.

SKILL documentation lookup is not execution of the documented function:
`find_skill(query, *, mode='fuzzy', limit=50, include_desc=False, source_dir=None,
cache_dir=None)` and `get_skill_more_info(func_name, *, source_dir=None,
cache_dir=None)` read/search documentation, may discover/transfer/cache files.
`search_docs(query, *, limit=10, doc_roots=None, cache_dir=None,
rebuild_index=False)` builds/queries a documentation index; `doc_info(doc_roots=None)`
inspects documentation metadata. They have no exposed per-call timeout argument;
remote discovery can use transport commands and errors depend on that layer.
These operator audit tools are not evidence that any searched SKILL API has been
implemented or safely wrapped in Bridge.

## Minimal proposed typed ADO operations

This is a **proposed adapter contract**, not a claim these functions exist in
Bridge. L1/L3 must implement and test missing adapters or fail closed. Caller
inputs are data only; no script, expression, callback, shell string or reflection.
Fixed vetted SKILL templates are internal worker implementation, never accepted
from an LLM, constraint, memory entry or plugin result. Reject arbitrary-SKILL
operations themselves, not merely the words system/sh in a submitted script.

| Proposed operation | Minimal typed input/output | Existing support and restriction |
|---|---|---|
| `read_placement` | CellviewRef -> complete PlacementSnapshot, units, provenance | read_geometry partly supports; strict complete snapshot/units/path/precision/close lifecycle adapter missing. No GUI-current-target fallback. |
| `read_terminals` | CellviewRef -> transformed terminal/pin/via geometry and nets | Missing layout facade; needs separately reviewed fixed read template. |
| `read_layout_snapshot` | CellviewRef -> canonical complete data + SHA-256 | Missing full fidelity hash; include hierarchy/PCell/input identities. Required for L3 final gate, not approximated by placement. |
| `append_path` | target, net ID, validated LPP, width, point list, owned route ID -> geometry receipt | create_path supplies shape rendering only; net association/ownership/postcheck adapter missing. |
| `append_via` | target, net ID, configured via ID, xy, enum orientation, typed numeric params -> receipt | via_by_name usable only with fixed cv and vetted parameter renderer; no raw via_params_expr/cv_expr. |
| `append_pin` | target, existing net ID, validated pin shape and direction -> receipt | Missing layout pin facade; label helper is not a substitute. |
| `append_label` | target, vetted LPP, bounded text, xy and enumerated display settings -> receipt | create_label fixed-builder subset; optional display, not connectivity proof. |
| `remove_owned_routes` | target, previous result hash, exact owned route IDs -> receipts | Missing. Never use clear_routing, delete-selected or layer-wide delete; never move/delete manual instances. |
| `save_target` | exact target/revision -> saved snapshot receipt | Existing save is global-cv sensitive; adapter must bind/check target and read postconditions, serialize session access. |
| `export_verification_snapshot` | saved target hash, configured stream-map reference, run-relative artifact destinations -> export receipt | Local fixed XStream possible; remote high-level path has shell dispatch. Require explicit reviewed fixed transport capability, never expose raw commands or recovery_hook. Otherwise mark unsupported. |

Manual mode deliberately has no instance-create/move operation. The installed
default-parameter placement and mosaic builders are inventory items, not a reason
to let ADO alter manual placement. Plugin floorplans remain proposals subject to
freeze; L4 defines no placement algorithm and does not implicitly authorize
arbitrary Bridge access by a generator.

A fixed-template dispatcher can internally use the raw transport only to send
code generated by an operation-ID allowlist. Private Python plugins still run
trusted executable code: plugins.load_function is not an OS sandbox. Restrict
their returned JSON; do not pass live Bridge/client/transport capabilities in
planner/generator contexts. The trusted role_commands execution hook similarly
is not a sandbox; deployments needing enforceable isolation must withhold Bridge
credentials/sockets and use process/OS permission boundaries for LLM roles.

All plugins use existing plugins.load_function and its module allowlist, with
file hashes for project-local code. Parameters use existing validate_value /
validate_values / number_value (including case-sensitive scaling), then geometry
adds finite, unit, grid, bbox, cardinality and enum checks. core.local_path confines
plans/handoffs/reports to the run or project. It intentionally rejects external
paths: L1 external private rule/run directories need a separately explicit
absolute-path validation policy, not a weakened local_path or public path copy.
Rules/runsets stay private; only hashes and approved numeric tech summaries
enter handoffs/prompts. Handoffs use core.write_json and SHA-256; raw report
bytes and actually consumed rule files are hashed, not regenerated descriptions.

Freeze artifacts are review gates. Planner output is JSON only; it cannot write
SKILL, call Bridge, submit tools, change floorplan, approve its own constraints,
or certify its own proposal. Reject unknown operation IDs/fields and names with
code-like content; bounded schema validation precedes any execution. Associate
layout with schematic run/netlist hash and preserve strict state transitions.
L3 VERIFIED requires DRC and LVS clean for the same full saved snapshot hash and
unchanged canonical placement hash. Infrastructure failure is NEEDS_ATTENTION,
not a fabricated clean report. No new transition is implemented in L0.

## Review decisions before L1/L3

1. Accept canonical content hashing separate from export timestamps for L1.
2. Choose whether fixed internal SKILL templates are acceptable: banning every
   SKILL transport call literally would prohibit the entire inspected Bridge.
   The recommended boundary bans arbitrary input and permits only reviewed fixed
   templates dispatched by typed operation ID.
3. Authorize or reject fixed transport shell commands separately from native
   layout primitives. Existing remote GDS export is not shell-free; do not quietly
   allow system/csh/raw command hooks. No safe stream-in facade was found.
4. Decide where missing units/terminals/pins/net-ownership/full-snapshot adapters
   will live and how exact Bridge source hashes are pinned. Unsupported operations
   must fail closed, not fall back to generated SKILL from an LLM.
5. Confirm verification locking/snapshot lifetime while the user has an editable
   GUI open; detection before/after an operation alone is not isolation.

## L0 verification and stop boundary

This stage changes only this document. Inventory was checked against installed
source bytes and callable definitions; no package install, CIW mutation,
simulation, routing, DRC or LVS was executed. Existing ADO unit tests are run as
regression evidence, not proof of new layout capability. No new test code is
added because L0 explicitly requires no code changes. Future fake-backend tests
must cover forbidden-operation injection, timeout reconciliation, precise complete
snapshot parsing, timestamp-independent placement comparison, owned-route-only
fixes, and same-snapshot verification. STOP after L0 PR; do not implement L1-L4
until reviewed and explicitly continued.
