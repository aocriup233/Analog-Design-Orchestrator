"""Interface stub, NOT a functioning Bridge reader or placement algorithm."""


def read_placement(context):
    if context["operation"] != "read_placement":
        raise ValueError("Unsupported typed operation")
    raise NotImplementedError(
        "Implement an audited, read-only Bridge adapter with complete precision, "
        "schema_version=2, instance/master/xy/orientation/bbox, params object and "
        "canonical params_sha256, authoritative units, pins(name/layer/bbox), "
        "labels(text/layer/bbox), pr_boundary polygon points. Empty lists must "
        "mean absent geometry, not a parser failure. Use ValueError for known "
        "input rejection and RuntimeError for unknown Bridge state. "
        "Do not use rounded summaries, arbitrary SKILL or shell fallbacks."
    )
