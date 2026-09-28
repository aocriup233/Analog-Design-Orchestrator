"""Load a user-owned reusable function by module name or project-local .py path."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Callable

from .core import local_path


def load_function(specification: str, project_root: Path) -> Callable:
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
        module = importlib.import_module(location)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError(f"Plugin target is not callable: {specification}")
    return function
