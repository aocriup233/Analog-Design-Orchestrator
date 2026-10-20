# Analog Design Orchestrator (ADO)

**Reproducible, reviewable handoffs for analog IC design.** ADO coordinates netlist preparation, simulation, and analysis through deterministic Python workers. The designer or an external controller makes circuit decisions; workers exchange JSON artifacts pinned by SHA-256.

[简体中文](README.zh-CN.md) · [MIT License](LICENSE)

> **Provisional `0.2.0-1015` snapshot.** This branch is for review, not a certified PDK, cluster, or physical-design release. The package and CLI remain named `analog-agent`; the Python package version is `0.2.0.dev1015`. No PyPI publication is implied.

![ADO architecture and review gates](docs/architecture-workflow.svg)

## Architecture and ownership

| Boundary | Responsibility | Durable evidence |
|---|---|---|
| Decision layer | User or external LLM selects a revision, testbench and candidates, then reviews the outcome. Proposals never auto-submit. | Campaign points and decisions |
| Netlist worker | Renders a project-defined block and testbench; never launches a simulator. | `netlist_result.json`, immutable `input.scs`, input hashes |
| Simulation worker | Builds the deck, stages/submits/polls/retrieves, verifies scheduler **and** simulator results, and owns remote cleanup. | Submission/retrieval receipts, `simulation_result.json` |
| Analysis worker | Measures verified results against project rules; never reruns Spectre implicitly. | `analysis_result.json`, plots, verdict and review |
| Optional layout workflow | Links a reviewed schematic run, freezes constraints and floorplan, then uses audited typed adapters for routing and DRC/LVS. | Separate `layouts/<id>/` handoffs and hashes |

`runs/<id>/` and `layouts/<id>/` are separate state machines. Stage-entry hash checks refuse changed pinned inputs without advancing state. A missing or conflicting submission receipt is **UNKNOWN_STATE**, not permission to submit again. `analog-agent recover RUN_DIR` inspects interrupted work without submitting; reconciliation must establish the actual remote job before retry. Task monitoring reports stalls without automatic resubmission. Bounded retries preserve old evidence in new attempt directories; see [failure recovery](docs/failure-recovery.md) and [task safety](docs/task-safety.md).

Public code contains no PDK rules, site paths, credentials or circuit-specific policy. User-owned `private/` provides those integrations and must be ignored in a project repository. `private.example/` documents interfaces, not a working cluster or Virtuoso adapter. Project-local Python plugins and `workflow.role_commands` are trusted executable configuration, not a sandbox. Only reviewed memory enters bounded decision context.

## Included in this snapshot

- **Safety S1–S3:** read-only `doctor`; `verify_inputs` and task monitoring; classified failures, capped retries and append-only attempts. **ADO-01:** state-first simulation CLI reports legal `STAGED` runs and reconciles uncertain evidence. **ADO-02:** typed layout adapters receive a versioned, run-confined context.
- **Design exploration:** bounded campaigns, optional constrained multi-objective BO, reviewed memory and offline replay. **Layout L1–L4:** linked runs and manual floorplan; frozen constraints; reviewed routing/DRC/LVS loop; proposal-only floorplan plugin.
- Immutable `CREATED → NETLIST_READY → STAGED → SUBMITTED → RUN → DONE → RETRIEVED → VERIFIED → ANALYZED` handoffs, with legal short-job skips, explicit failure states and reviewed next-run decisions. `PASS` means only that the **project's** rules passed.
- Reusable circuit blocks and independent testbenches, DC/AC/STB/transient rule files, generic waveform/eye/FFT tools, and project-defined metric plugins. Legacy single-file netlists remain supported.
- `bridge`, `local`, asynchronous `lsf`, and `external` simulation backend interfaces. LSF and external execution need a site-tested private adapter. `external` stages a deck but does **not** submit it.
- Durable campaigns with bounded parallel points, explicit review gates, read-only replay, reviewed private memory, and an optional [multi-objective Bayesian proposer](docs/mobo.md) (`.[bo]`). A proposer only suggests points; the user still runs `campaign add`.
- Read-only `doctor`, stage-entry hash gates, heartbeat monitoring and classified recovery. These checks do not certify a live cluster or PDK.
- An **opt-in layout framework**: linked layout runs, reviewed constraints, manual or plugin-proposed floorplans, deterministic diff/score, JSON-only routing plans and typed DRC/LVS handoffs. Its tests use fake adapters. Real deployment still needs audited private placement, routing, LVS-source and verification adapters; ADO does not claim a live inverter layout has passed DRC/LVS.

## Install and start

Use Python 3.10+ in an existing environment. ADO does not install Spectre, Virtuoso, a PDK or a license.

```sh
python -m pip install -e .
# Optional: python -m pip install -e '.[analysis]'
# Optional: python -m pip install -e '.[bo]'
analog-agent doctor
analog-agent new project.example.json
```

`project.example.json` is a repository-relative RC fixture. `new` prepares a netlist run; it does **not** prove simulator availability. Do not call `submit` until your backend and private dependencies are configured. For a separate project, copy the *shape* of `project.example.json` and `private.example/`, then supply your own block, testbench, rules, metrics, model includes and backend. Never put credentials in JSON, command arguments, artifacts or Git.

For a configured project, the manual sequence is:

```sh
analog-agent new PROJECT.json --testbench ac
analog-agent submit RUN_DIR
analog-agent status RUN_DIR
analog-agent resume RUN_DIR       # asynchronous backend only; one poll per call
analog-agent analyze RUN_DIR      # after VERIFIED
analog-agent decide-run RUN_DIR --decision iterate --to netlist --reason "reviewed evidence"
```

`analog-agent duty RUN_DIR` shows the role still responsible. After an interruption, use `inspect` and `recover` first; queue acceptance or process exit is not proof of success. For a legal external-worker `STAGED` handoff, `submit` reports the staging result and next submit obligation without creating submission files. Evidence conflicts return `UNKNOWN_STATE` with exit code 3 and require reconciliation.

## Campaign and layout entry points

```sh
analog-agent campaign new PROJECT.json
analog-agent campaign add CAMPAIGN_DIR POINTS.json
analog-agent campaign run CAMPAIGN_DIR --max-seconds 3600
analog-agent campaign status CAMPAIGN_DIR
```

Each cycle yields a comparison for review. A private proposer or optional MOBO proposer may suggest the next batch but cannot submit it. [Offline replay](examples/replay_history.json) checks recorded decisions without creating runs or launching a simulator. After context/token loss, `campaign doctor` and `campaign brief` restore the durable state before continuation.

Layout starts with `analog-agent layout prepare SCHEMATIC_RUN_DIR` after schematic verification and review gates. The user freezes [constraints](docs/layout-constraints.md), compares and freezes the live [floorplan](docs/layout.md), explicitly releases routing, then uses [typed routing and verification](docs/layout-routing.md). A [plugin-generated floorplan](docs/layout-floorplan-plugin.md) is a proposal, never automatically placed or frozen. DRC/LVS clean requires matching saved cellview and LVS-source evidence, not just a successful tool exit. Live tool adapters and foundry rules remain private and must be independently validated.

PEX and post-layout simulation are **not yet an integrated ADO stage**. A private extractor can in principle produce a parasitic netlist for a separate new simulation run, but the current layout state machine does not pin a cellview → PEX → post-layout-result hash chain or provide its review gate. Do not present that manual composition as a verified end-to-end layout flow.

## Documentation and verification

- [Role instructions](AGENTS.md), [netlist](agents/netlist.md), [simulation](agents/simulation.md), [analysis](agents/analysis.md)
- [Safety doctor](docs/doctor.md), [task monitoring and hash gates](docs/task-safety.md), [failure recovery](docs/failure-recovery.md)
- [MOBO contract](docs/mobo.md) and [synthetic benchmark](docs/bench_mobo.md)
- [Layout bridge audit](docs/design/layout-bridge-api.md), [layout contracts](docs/layout.md), [simulation-cost design](docs/design/simulation-cost.md)

Run `python -m unittest discover -s tests -q`. CI also runs the read-only doctor and uses hash-pinned test dependencies. Fake-backend tests validate public contracts; they cannot replace site-specific login, transfer hashes, simulator logs, retrieval, remote cleanup or native DRC/LVS evidence. This snapshot is for review before a stable `0.2.0` decision.

Technology-independent Python gm/Id lookup, sizing, desktop UI and optimizer interfaces: [guide](docs/gmid.zh-CN.md). The examples in examples/gmid are synthetic. Keep all real process tables in project-private storage.

Install `.[gmid]` for the optional lookup dependencies. Single-corner,
single-temperature tables are supported; uncharacterized conditions are never
extrapolated. MOBO can use private lookup requests as cold-start seeds.
See the [public package boundary and release checks](docs/public-release.md).
