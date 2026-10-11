"""Echo a user-supplied placement; no placement algorithm or EDA operations."""
import copy


def generate(context):
    placement = context.get("user_placement")
    if not isinstance(placement, dict):
        raise ValueError("Supply a reviewed placement with --placement; this example does not place anything")
    result = copy.deepcopy(placement)
    result["source"] = "plugin"
    result["plugin_sha256"] = context["plugin_sha256"]
    return result
