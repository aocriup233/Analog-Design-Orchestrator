"""Interface stub, NOT a functioning Bridge reader or placement algorithm."""


def read_placement(context):
    if context["operation"] != "read_placement":
        raise ValueError("Unsupported typed operation")
    raise NotImplementedError(
        "Implement an audited, read-only Bridge adapter with complete precision, "
        "instance/master/xy/orientation/bbox and authoritative units. "
        "Do not use rounded summaries, arbitrary SKILL or shell fallbacks."
    )
