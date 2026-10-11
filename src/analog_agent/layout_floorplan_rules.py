"""Validate user-owned placement rules; no placement algorithm or PDK defaults."""
from __future__ import annotations

from .layout_config import identifier
from .layout_floorplan import ORIENTATIONS, instance_identifier
from .parameters import number_value, validate_value

RULES_PATH = "private/layout/floorplan_rules.json"


def validate_rules(document, *, leading=False):
    def fields(value, required, optional=()):
        if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
            raise ValueError("Invalid floorplan rule fields")

    def number(value, label, minimum=0, *, positive=False):
        result = number_value(validate_value(label, value, {"type": "number"}))
        if result < minimum or (positive and result <= minimum):
            raise ValueError(f"Invalid {label} bound")
        return result

    def box(value):
        if not isinstance(value, list) or len(value) != 2 or any(not isinstance(p, list) or len(p) != 2 for p in value):
            raise ValueError("Rule bbox must have two xy corners")
        for p in value:
            for v in p:
                number(v, "coordinate_nm", float("-inf"))
        if any(number_value(str(value[0][a])) >= number_value(str(value[1][a])) for a in (0, 1)):
            raise ValueError("Rule bbox must have positive extents")

    def names(value):
        if not isinstance(value, list) or not value or len(value) > 100000:
            raise ValueError("Rule members must be a nonempty bounded list")
        canonical = [instance_identifier(n, allow_leading_digit=leading) for n in value]
        if len(set(canonical)) != len(canonical):
            raise ValueError("Duplicate rule members")

    keys = {"schema_version", "units", "rows", "regions", "symmetry_axes", "matching_patterns", "spacing", "guard_rings", "keep_outs", "pin_sides", "targets"}
    fields(document, keys, {"objective_weights"})
    if type(document["schema_version"]) is not int or document["schema_version"] != 1 or document["units"] != "nm":
        raise ValueError("Floorplan rules require schema_version=1, units=nm")
    for key in keys - {"schema_version", "units", "targets"}:
        values = document[key]
        if not isinstance(values, list) or len(values) > 100000:
            raise ValueError(f"{key} must be a bounded list")
        ids = set()
        for item in values:
            required = {
                "rows": {"id", "bbox_nm", "instances", "orientation"},
                "regions": {"id", "bbox_nm", "instances"},
                "symmetry_axes": {"id", "direction", "coordinate_nm"},
                "matching_patterns": {"group_id", "style"},
                "spacing": {"id", "members", "min_nm"},
                "guard_rings": {"id", "members", "width_nm", "spacing_nm", "layers"},
                "keep_outs": {"id", "bbox_nm"},
                "pin_sides": {"pin", "side"},
            }[key]
            fields(item, required)
            identity = item.get("id", item.get("group_id", item.get("pin")))
            identifier(identity, key, allow_leading_digit=leading)
            if identity in ids:
                raise ValueError(f"Duplicate {key} identity: {identity}")
            ids.add(identity)
            if "bbox_nm" in item:
                box(item["bbox_nm"])
            for member_key in ("instances", "members"):
                if member_key in item:
                    names(item[member_key])
            if key == "rows" and (not isinstance(item["orientation"], str) or item["orientation"] not in ORIENTATIONS):
                raise ValueError("Invalid row orientation")
            if key == "symmetry_axes":
                if not isinstance(item["direction"], str) or item["direction"] not in {"x", "y"}:
                    raise ValueError("Invalid symmetry axis")
                number(item["coordinate_nm"], "coordinate_nm", float("-inf"))
            if key == "matching_patterns" and (not isinstance(item["style"], str) or item["style"] not in {"none", "common-centroid", "interdigitated"}):
                raise ValueError("Invalid matching style")
            if key == "spacing":
                number(item["min_nm"], "min_nm")
            if key == "guard_rings":
                number(item["width_nm"], "width_nm", positive=True)
                number(item["spacing_nm"], "spacing_nm")
                if not isinstance(item["layers"], list) or not item["layers"] or not all(isinstance(v, str) for v in item["layers"]) or len(set(item["layers"])) != len(item["layers"]):
                    raise ValueError("Guard-ring layers must be distinct and nonempty")
                for layer in item["layers"]:
                    identifier(layer, "guard-ring layer")
            if key == "pin_sides" and (not isinstance(item["side"], str) or item["side"] not in {"left", "right", "top", "bottom", "any"}):
                raise ValueError("Invalid pin side")
    fields(document["targets"], set(), {"aspect_ratio", "area_nm2"})
    for key, limits in document["targets"].items():
        fields(limits, {"min", "max"})
        if number(limits["min"], key, positive=True) > number(limits["max"], key, positive=True):
            raise ValueError("Reversed floorplan target bounds")
    if "objective_weights" in document:
        weights = document["objective_weights"]
        fields(weights, set(), {"area", "aspect_ratio", "wirelength", "symmetry", "matching", "power"})
        for key, value in weights.items():
            number(value, f"weight.{key}")
    # Validate without normalizing/reinterpreting user preferences.
    return document
