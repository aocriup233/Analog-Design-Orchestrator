# Analog Design Orchestrator (ADO)

**An evidence-driven workflow for analog IC design, simulation, and analysis.**

[简体中文](README.zh-CN.md)

ADO separates circuit decisions from EDA operations. A designer or an external LLM controller proposes a circuit revision; three process-isolated workers prepare its netlist, execute configured analyses, and measure the results. Each step writes a durable, hash-checked handoff under a unique run directory. The reusable engine contains no CTLE topology, foundry PDK, cluster address, or circuit-specific performance formula.

The Python distribution and CLI remain named `analog-agent` for compatibility. **ADO is the project name, not a new CLI command.**

## What is implemented

- Separate design `input.scs` and simulator-owned `simulation.scs`, with reusable DC, AC, and transient rule files.
- Bridge, local Spectre, and asynchronous LSF *backend interfaces*. LSF requires a working private site adapter; the supplied adapter is a template, not a configured cluster connection.
- Durable `CREATED → NETLIST_READY → STAGED → SUBMITTED → RUN → DONE → RETRIEVED → VERIFIED → ANALYZED` transitions. A short job may skip `RUN`; a reported failure enters `FAILED`. `resume` makes one poll and continues retrieval and verification when the scheduler reports `DONE`.
- Generic waveform windows, overlays, samples, reference comparisons, eye diagrams, and FFTs; project plugins provide circuit-specific metrics.
- Public configuration plus ignored, user-owned private configuration, scripts, and skills.

Workers are deterministic Python processes, **not three built-in autonomous LLMs**. An LLM controller may use their artifacts to choose the next design. Automatic expert-knowledge updates and a ready-to-use adapter for every cluster are not implemented yet.

## Installation and a first run

Use Python 3.10+ in an environment that can run your selected simulator. For Cadence Bridge work, use an environment with `virtuoso_bridge` already installed; ADO does not install Spectre, Virtuoso, a PDK, or a license. Install the package from this directory:

```sh
python -m pip install -e .
# Optional plotting tools: python -m pip install -e '.[analysis]'
```

`project.example.json` is an RC smoke-test configuration whose paths are relative to this repository. To inspect netlist generation without launching a simulator:

```sh
cp project.example.json project.json
analog-agent new project.json
```

The command prints the new `runs/<run_id>` path. Do not run `submit` until the chosen backend and its simulator or private site adapter are configured. For a separate user project, place its own `project.json`, design template or generator, rule files, and `private/` tree together; update every path copied from the repository example.

## What each user configures

| Scope | Configure | Where |
|---|---|---|
| Circuit project | Design template **or** `netlist.generator`, legal parameters and candidates, analyses, saved signals, metric plugins, and project-defined acceptance rules | `<project>/project.json`, templates, rule/plugin files |
| Private global environment | Simulator executable or Bridge profile; for LSF, host/transfer topology, queue/resources, remote paths, PDK model locations and sections | `<project>/private/global/config/` and reusable private scripts |
| Netlist role | PDK-valid devices, pin order, includes, and any circuit-specific generator | `<project>/private/agents/netlist/` |
| Simulation role | DC/AC/tran/custom rule JSON, execution backend and site adapter, polling/retrieval/cleanup behavior | `<project>/private/agents/simulation/` and `simulation` config |
| Analysis role | Circuit-specific measurement functions and thresholds; optional waveform requests | `<project>/private/agents/analysis/` and `metrics`/`rules` config |

Start with `private.example/` for file shapes. Four optional `private/**/config/overrides.json` files are merged into the public project configuration at runtime. Their contents are not copied into `runs/<run_id>/config.json`; the run records their SHA-256 hashes and refuses to continue if they change mid-run. Other private files, including example `pdk.json` and `transfer.json`, are **not read automatically**: the user's generator or site adapter must load them. Keep credentials in an interactive prompt or an operating-system secret facility, never in configuration, command arguments, artifacts, or the repository. If the user project has its own Git repository, ignore its `private/` and `runs/` directories there too.

For a remote site, configure and verify the path in this order: (1) login hops and the actual remote identity/project directory; (2) PDK/model visibility and the netlist's device/include mapping; (3) upload/download transport with hashes; (4) LSF queue, resources, job ID, and status polling; (5) Spectre command, mode, output location, and success checks; (6) retrieval, parsed measurements, and post-verification cleanup. An SSH-to-Telnet-to-LSF site implements those hops in its private adapter and scripts. Capture the real output at each consequential step; a successful login or simulation must not be inferred from a command exit alone.

### Choose an execution backend

| Backend | Required user setup | Behavior |
|---|---|---|
| `bridge` | Working Virtuoso Bridge environment/profile and Spectre access | Synchronous submit and verified parsed result |
| `local` | Local Spectre executable (`simulation.spectre_cmd`) | Synchronous submit and verified parsed result |
| `lsf` | Private factory at `simulation.adapter`; implement all six site methods | Asynchronous submit, then one poll per `resume` call |

For LSF, a private override may select the adapter without publishing site details:

```json
{
  "simulation": {
    "backend": "lsf",
    "adapter": "private/agents/simulation/scripts/site_lsf_adapter.py:create",
    "remote_cleanup": "manual"
  }
}
```

Copy [the adapter template](private.example/agents/simulation/scripts/site_lsf_adapter.py) and implement `stage`, `submit`, `poll`, `retrieve`, `verify`, and `cleanup` using reusable site functions. `stage` must confirm the deck hash; `submit` returns a real job ID; `poll` distinguishes submitted/RUN/DONE/failure; `retrieve` lists local artifacts and their SHA-256 (provide `remote_sha256` for a cross-end check); `verify` must confirm scheduler **and** simulator success and return parsed data. Use the run ID as a remote idempotency key so an interruption during submission cannot create a duplicate job. The core validates transitions and local artifacts, and compares the remote hash when provided, but it cannot prove that an unimplemented site adapter actually logged in or transferred files.

For any backend, choose `simulation.rules` (for example the public [DC](agents/simulation/config/dc.json), [AC](agents/simulation/config/ac.json), or [tran](agents/simulation/config/tran.json) rules), `save_signals`, mode, timeout, and PSF ASCII output. A custom Python rule renderer can add analyses supported by your installation. PDK includes belong in the project design/generator or configured include files, not in the generic engine.

## Operate a run

```sh
analog-agent new project.json --iteration 0 --set R=2k
analog-agent submit RUN_DIR
analog-agent status RUN_DIR
analog-agent resume RUN_DIR       # repeat for asynchronous LSF work
analog-agent analyze RUN_DIR      # only after local verification
```

`submit` finishes in one call for Bridge/local. For LSF, keep the returned run and invoke `resume` again after the job changes state; `analog-agent run project.json` also stops after an asynchronous submission rather than blocking indefinitely. Every run preserves its task history, input hashes, phase handoffs, logs or retrieved artifacts, and analysis report. Resume a run in the **same path environment** in which it was created (for example, do not switch a run's stored `/mnt/e/...` paths to Windows `E:\...` mid-run).

`workflow.submission` selects `manual` or `auto`; `return_mode` selects compact `summary` or full parsed data; `cleanup` selects `never` or local raw-file removal `after_success`. `simulation.remote_cleanup` is `never`, `manual`, or `after_verified`; `cleanup-remote RUN_DIR` calls the private adapter only after local verification. Do not treat a queue acceptance or process exit alone as simulation success.

### Durable parallel campaigns

For a user/analysis-AI-selected sweep, add an optional `campaign` block to `project.json`. These are execution limits, **not circuit-design defaults**:

```json
{
  "campaign": {
    "max_parallel": 2,
    "max_points_per_cycle": 16,
    "max_cycles": 10,
    "max_total_points": 100,
    "poll_interval_s": 10,
    "metric_directions": {"gain_1g_db": "max"}
  }
}
```

Create a campaign, review a points file such as [the RC example](examples/campaign_points.json), and run one cycle:

```sh
analog-agent campaign new project.json
analog-agent campaign add CAMPAIGN_DIR examples/campaign_points.json
analog-agent campaign run CAMPAIGN_DIR --max-seconds 3600
analog-agent campaign status CAMPAIGN_DIR
```

`campaign run` schedules at most `max_parallel` points at once and polls locally without an LLM call per job. Each finished run is retrieved, verified, and analyzed promptly; the cycle comparison is written only when every point is analyzed or has failed. The report lists parameters, metrics, verdicts, failures, and optional Pareto sets. No score, initial gₘ/Iᴅ point, search algorithm, or universal specification is built in. `metric_directions` only defines optional Pareto directions; the user and analysis AI choose the next cycle or final result.

After review, call `campaign add CAMPAIGN_DIR NEXT_POINTS.json --decision "reason" --select POINT_ID` for another cycle, or `campaign finish CAMPAIGN_DIR --decision "reason" --select POINT_ID`. A private function configured as `campaign.proposer` (`private/agents/analysis/scripts/site_proposer.py:propose`) can generate an initial or next-cycle proposal with `campaign propose CAMPAIGN_DIR`; inspect its saved JSON and explicitly `add` it. The [private proposer template](private.example/agents/analysis/scripts/site_proposer.py) intentionally contains no PDK method or automatic optimizer.

If a chat loses context or tokens, use `campaign doctor CAMPAIGN_DIR` to inspect durable point/run states and `campaign brief CAMPAIGN_DIR` for a compact AI handoff; then `campaign run` or `campaign step` continues without old chat history. `campaign pause`/`resume` stop/restart local scheduling, not already-submitted remote jobs. An interrupted submission with no local receipt is marked uncertain and **never blindly resubmitted**. An LSF site adapter may implement optional `reconcile(run, config, staged, intent)` to look up the real job by run ID; `analog-agent reconcile RUN_DIR` adopts that verified receipt, after which `campaign retry CAMPAIGN_DIR POINT_ID` re-enables the safe step. Without that lookup, inspect the scheduler manually and do not retry. A cycle freezes the public config, private overrides, and declared input-file hashes (template, rules, plugins, includes); list additional site files in `campaign.dependencies`. Change circuit configuration between cycles, not midway through one. Campaigns live under ignored `<project>/campaigns/`; ignore that directory in an independent project too. Run and resume each campaign in the same Windows or WSL path environment.

For sites that require an interactive or otherwise out-of-process executor, set `simulation.backend` to `external`. The same `campaign step/run/doctor` workflow stages a hashed deck and returns `AWAITING_EXECUTION` without submitting it. A private site simulation worker then performs transfer, submission, retrieval, and verification, writing the normal run handoffs and reaching `VERIFIED`; the next `campaign step` invokes the analysis role and continues the cycle. The core never embeds SSH, Telnet, LSF, or credential handling, and the site worker must preserve the run's state and hash contract. `external` is a resumable coordination boundary, not unattended simulation.

If analysis inputs must be corrected after a run is verified, keep the original campaign for audit and create a new one with the corrected inputs. `campaign attach CAMPAIGN_DIR POINT_ID VERIFIED_RUN --reason "..."` checks the project, exact point, netlist/deck hashes, and simulation receipt before reusing that run; it never resubmits the job.

## Results, waveform tools, and role boundaries

The netlist worker owns `netlist_result.json`; the simulation worker owns `simulation_plan.json`, staging/submission/poll/retrieval handoffs, and finally `simulation_result.json`; the analysis worker owns `analysis_result.json`. The design netlist cannot be edited after its hash is handed off. Project-specific metric hooks receive parsed data and return scalar values. A `PASS` means **only that the user's configured `rules` passed**; it does not imply a universal circuit specification.

Use [waveform_tool.py](agents/analysis/scripts/waveform_tool.py) with the JSON requests under `agents/analysis/config/` for separate windows, overlays, interpolated points, reference lines, eye folding, and FFT. NumPy and Matplotlib are optional dependencies needed for these plotting functions. Keep large waveform arrays in artifacts; show an LLM only the metrics, verdicts, relevant plot paths, and requested excerpts.

`AGENTS.md` routes project-local skills to the roles. These skills and the common scripts are intended to remain stable; circuit- and PDK-specific knowledge belongs in user projects. A model-driven design loop and evidence-backed expert memory can be added around the artifacts without changing the deterministic execution contract.

## Verification and release boundaries

Run the local tests with `python -m unittest discover -s tests -q`. They cover synchronous handoffs, rule generation, waveform operations, and a **fake** LSF adapter with cross-process resume; they do not certify any particular user's real cluster. Before publishing a site configuration, verify login/identity, PDK readability, file-transfer hashes, scheduler job ID and status, simulator logs, result retrieval, and cleanup with captured output from that site.

The CLI and artifact format are still evolving (`pyproject.toml` currently declares version `0.1.0`). The project is released under the [MIT License](LICENSE).
