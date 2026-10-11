"""L3 contract stub, not a deployed Virtuoso router or verifier.

dispatch(request) receives operation, exact target, timeout_s and payload.
Implement only docs/layout-routing.md operations using separately audited fixed
APIs. Never accept SKILL/shell/expression strings, move manual instances or broadly
delete shapes. Full saved content must be hashed, not screenshots or placement.
Verification uses fixed argv, shell=False, process-group timeout/cancellation,
and retains native reports plus strict schema-v2 normalization: exact native
success-marker location and diagnostic counters, including unknown lines and
truncation. Configure audited stage-specific verification_success_markers;
never infer clean from an empty violation list alone. Each violation supplies
repair={class: routing|placement|manual|unknown, reason: nonempty text}, using
audited private rule mapping; never label device spacing as wire-fixable merely
because its rule name mentions spacing. Both adapters are loaded
through plugins.load_function. This example deliberately fails closed.
"""


def dispatch(request):
    raise NotImplementedError("Complete audited typed layout/verification adapter required; no raw fallback")
