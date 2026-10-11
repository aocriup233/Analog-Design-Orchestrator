"""Private immutable-Spectre -> CDL and complete-normalization contracts.

No current Virtuoso schematic export. Generate stable bytes from the request's
hash-pinned simulation copy and explicit top only. Declare any PDK terminal/model
or parameter mapping files in lvs_source.dependencies. Never silently omit devices,
pins, parameters, include dependencies, hierarchy or unresolved expressions.
Both functions receive the standard operation/target/timeout_s/payload envelope.
See docs/layout-routing.md. These are deliberately unsupported template functions.
"""


def generate(request):
    raise NotImplementedError("Implement deterministic CDL generation from immutable simulation bytes")


def normalize(request):
    raise NotImplementedError("Implement complete independent Spectre/CDL electrical normalization")
