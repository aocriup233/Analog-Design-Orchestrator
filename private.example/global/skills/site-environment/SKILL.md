---
name: site-environment
description: Configure private EDA host roles, transfer procedures, simulator profiles, and PDK references for a local Analog Agent installation.
---

# Site environment

This skill is a copyable private template. Keep the actual skill under ignored `private/global/skills/site-environment/` and fill it with your site's verified host/path rules. Store connection settings in the bridge's environment configuration; never put passwords in scripts, prompts, or run artifacts.

Use `private/global/config/overrides.json` for runtime overrides, `transfer.json` for site-specific transfer/scheduler metadata, and `pdk.json` for model and device notes. If bridge transfer is insufficient, implement a private adapter under `private/global/scripts/` and bind it through `workflow.role_commands.simulation`. Preserve actual command output, job IDs, hashes, and retrieval evidence.
