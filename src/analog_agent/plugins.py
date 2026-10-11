"""Load a user-owned reusable function by module name or project-local .py path."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Callable
import functools

from .core import local_path


_FORBIDDEN = {"builtins", "os", "posix", "nt", "subprocess", "shutil", "importlib"}


def load_function(specification: str, project_root: Path, config: dict | None = None) -> Callable:
    if ":" not in specification:
        raise ValueError("Function reference must be module:function or path.py:function")
    location, function_name = specification.rsplit(":", 1)
    if location.endswith(".py"):
        path = local_path(project_root, location)
        if not path.is_file():
            raise FileNotFoundError(path)
        module_spec = importlib.util.spec_from_file_location(f"analog_agent_plugin_{path.stem}", path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"Cannot load plugin: {path}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        allowed = (config or {}).get("plugins", {}).get("allowed_modules", ["analog_agent.proposers"])
        if (location.split(".")[0] in _FORBIDDEN or not isinstance(allowed, list)
                or not all(isinstance(item, str) and item for item in allowed)
                or not any(location == item or location.startswith(item + ".") for item in allowed)):
            raise ValueError(f"Plugin module is not allowed: {location}")
        module = importlib.import_module(location)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError(f"Plugin target is not callable: {specification}")
    target = function
    while isinstance(target, functools.partial):
        target = target.func
    owner = getattr(target, "__module__", type(target).__module__)
    if owner.split(".")[0] in _FORBIDDEN:
        raise ValueError(f"Forbidden plugin callable: {owner}")
    return function
