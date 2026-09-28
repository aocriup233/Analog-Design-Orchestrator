---
name: analog-project
description: Work on this Analog Agent project's shared configuration, artifact contracts, and private site adapters; use when coordinating its three roles.
---

# Analog project

Read `AGENTS.md` for the role boundaries. Use `project.json` and `agents/*/config` for shareable circuit and analysis rules. Site hosts, transfer topology, PDK knowledge, and credentials belong under ignored `private/` or the existing bridge environment, using `private.example/` only as a starting shape.

Run `common/scripts/inspect_workspace.py PROJECT.json` for a redacted inventory. A run stores a public config snapshot and hashes of private override files; workers refuse a run if those private files change during it. Do not print private config contents in reports.

For simulation rule authoring, use the matching DC, AC, or tran skill under `agents/simulation/skills`. For waveform calculations and plots, use `agents/analysis/skills/waveform-toolkit`.
