---
name: campaign-review
description: Resume and review a durable multi-cycle analog design campaign without relying on prior chat context.
---

# Campaign review

Read `agents/analysis.md`. Start with `analog-agent campaign doctor CAMPAIGN_DIR` for run health and `analog-agent campaign brief CAMPAIGN_DIR` for the compact decision context. Read the latest `cycle-*_comparison.json` only when the batch is in `REVIEW`; raw waveforms remain in each run and should be opened only when a decision needs them.

Compare user-configured metrics, verdicts, failed jobs, and optional Pareto candidates. Do not invent a scalar score, a g_m/I_D starting point, or a circuit specification. Ask the user to resolve material tradeoffs. Record selected point IDs and rationale with `campaign add ... --decision ... --select ...` or `campaign finish ...`. A private `campaign.proposer` may create a proposal, but review it before adding the cycle.

If `doctor` reports an uncertain submission, do not re-submit it. Use a site adapter's verified `reconcile` lookup when available, or inspect the actual scheduler job with the user's site workflow. Do not treat queue acceptance as a completed simulation.
