"""Explicit ID bindings between user placement preferences and frozen L2 rules."""
from .parameters import number_value, validate_value


def check_consistency(rules, document):
    """Reject ambiguities/overrides before invoking any generation plugin."""
    frozen = {item["id"]: item for item in document["constraints"]}
    conflicts = []
    for axis in rules["symmetry_axes"]:
        linked = frozen.get(axis["id"])
        if linked is None or linked["kind"] != "symmetry":
            conflicts.append(f"symmetry_axes {axis['id']}: must bind a frozen symmetry constraint with the same ID")
            continue
        coordinate = number_value(validate_value("coordinate_nm", axis["coordinate_nm"], {"type": "number"}))
        expected = number_value(validate_value("coordinate_nm", linked["axis"]["coordinate_nm"], {"type": "number"}))
        if axis["direction"] != linked["axis"]["direction"] or coordinate != expected:
            conflicts.append(f"symmetry_axes {axis['id']}: proposed {axis['direction']}={coordinate} nm conflicts with frozen {linked['axis']['direction']}={expected} nm")
    for pattern in rules["matching_patterns"]:
        linked = frozen.get(pattern["group_id"])
        if linked is None or linked["kind"] != "matching":
            conflicts.append(f"matching_patterns {pattern['group_id']}: must bind a frozen matching constraint")
        elif pattern["style"] != linked["style"]:
            conflicts.append(f"matching_patterns {pattern['group_id']}: proposed {pattern['style']} conflicts with frozen {linked['style']}")
    if conflicts:
        raise ValueError("Floorplan rule/constraint conflicts: " + "; ".join(sorted(conflicts)))
    return {"status": "CONSISTENT", "symmetry_axes_checked": len(rules["symmetry_axes"]),
            "matching_patterns_checked": len(rules["matching_patterns"])}
