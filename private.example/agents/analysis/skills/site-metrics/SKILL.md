---
name: site-metrics
description: Calculate private circuit-specific design metrics from verified simulator results using reusable local metric functions.
---

# Site metrics

Copy this skill to `private/agents/analysis/skills/site-metrics/`. Implement circuit-specific metrics as `kind: python` functions; record signal names, units, analysis type, and formula. Use the public waveform toolkit for plotting and generic calculations. Do not infer values from incomplete or failed runs.
