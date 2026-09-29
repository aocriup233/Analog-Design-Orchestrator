---
name: knowledge-promotion
description: Review private campaign evidence and extract reusable, circuit-independent scripts or skills into the shared Analog Agent project.
---

# Knowledge promotion

The user project supplies design decisions and measured evidence, not a
function that publishes private knowledge. The main agent owns this review
and any public code change. A passing point alone is not evidence that a
method is reusable.

Start from reviewed campaign comparisons, recorded decisions, and the private
method that produced them. Separate the reusable operation from circuit
topology, PDK names, hosts, credentials, parameter values, metric thresholds,
and raw waveforms. If that boundary cannot be made clear, leave the method
private and state what further evidence is needed.

For a candidate shared script or skill, record which observations motivated
the extraction, the proposed generic input/output contract, and the behavior
that should remain private. Implement it through the normal reviewed code
workflow, add circuit-neutral offline tests, and verify an existing private
use case can call the shared function without changing its result. Validate
changed skills with the skill validator. Do not copy private files wholesale,
silently change a live campaign, or submit a simulator job to justify a
promotion. Keep public Git changes reviewable and reversible.
