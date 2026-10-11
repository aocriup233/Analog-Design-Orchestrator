"""One parameter contract for CLI, campaigns and deterministic netlisting."""

import math
import re

_ATOM = re.compile(r"[A-Za-z0-9_+./*()^<>=:,\-]+")
_NUMBER = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)")
_SCALE = {"": 1, "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3,
          "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}


def number_value(value: str) -> float:
    match = _NUMBER.fullmatch(value)
    if not match or match[2].lower() not in _SCALE:
        raise ValueError(f"Expected a finite numeric parameter, got {value!r}")
    result = float(match[1]) * _SCALE[match[2].lower()]
    if not math.isfinite(result):
        raise ValueError("Numeric parameter must be finite")
    return result


def validate_value(name: str, value, schema: dict | None = None) -> str:
    if value is None or isinstance(value, (bool, dict, list, tuple)):
        raise ValueError(f"Parameter {name} needs a scalar value")
    text = str(value)
    if (not _ATOM.fullmatch(text) or "//" in text or "/*" in text or "*/" in text
            or re.search(r"\b(?:include|ahdl_include|simulator)\b", text, re.I)):
        raise ValueError(f"Unsafe parameter value for {name}: {text!r}")
    schema = schema or {}
    kind = schema.get("type")
    if kind not in (None, "number", "enum"):
        raise ValueError(f"Invalid parameter type for {name}: {kind}")
    if kind == "enum":
        candidates = schema.get("candidates")
        if not isinstance(candidates, list) or not candidates or text not in [str(item) for item in candidates]:
            raise ValueError(f"Parameter {name} must match an enum candidate")
    if kind == "number" or any(key in schema for key in ("min", "max", "log")):
        numeric = number_value(text)
        bounds = {key: number_value(str(schema[key])) for key in ("min", "max") if key in schema}
        if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
            raise ValueError(f"Parameter {name} min exceeds max")
        if any((numeric < bound if key == "min" else numeric > bound) for key, bound in bounds.items()):
            raise ValueError(f"Parameter {name} is outside its bounds")
        if schema.get("log") and numeric <= 0:
            raise ValueError(f"Log parameter {name} must be positive")
    return text


def validate_values(values: dict, definitions: dict | None = None) -> dict[str, str]:
    if definitions is not None and values.keys() - definitions.keys():
        raise ValueError(f"Unknown parameters: {sorted(values.keys() - definitions.keys())}")
    return {name: validate_value(name, value, (definitions or {}).get(name))
            for name, value in values.items()}
