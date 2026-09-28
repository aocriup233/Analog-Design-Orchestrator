---
name: site-simulation
description: Run configured analyses using a private simulator host, scheduler, transfer path, and result retrieval procedure.
---

# Site simulation

Copy this skill to `private/agents/simulation/skills/site-simulation/` and document only verified host roles, transfer paths, and scheduler commands. Configure `simulation.backend=lsf` with a private `simulation.adapter` factory implementing the six methods in `private.example/agents/simulation/scripts/site_lsf_adapter.py`. The public worker owns durable state and JSON handoffs; the private adapter owns site execution, hashes, and parsing. Never report submission as completion. Download and verify local results before any remote cleanup. Do not put credentials in configuration, command arguments, or artifacts.
