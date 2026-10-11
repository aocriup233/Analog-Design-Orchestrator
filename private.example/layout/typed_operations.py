"""L3 contract stub, not a deployed Virtuoso router or verifier.

dispatch(request) receives operation, exact target, timeout_s and payload.
Implement only docs/layout-routing.md operations using separately audited fixed
APIs. Never accept SKILL/shell/expression strings, move manual instances or broadly
delete shapes. Full saved content must be hashed, not screenshots or placement.
Verification uses fixed argv, shell=False, process-group timeout/cancellation,
and retains native reports plus strict normalization. Both adapters are loaded
through plugins.load_function. This example deliberately fails closed.
"""


def dispatch(request):
    raise NotImplementedError("Complete audited typed layout/verification adapter required; no raw fallback")
