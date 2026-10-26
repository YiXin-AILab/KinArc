"""Load copied task modules without assuming a canonical task directory name."""

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def adapter(entry: Path) -> ModuleType:
    """Create an isolated package name for this task's internal implementation."""
    internal = entry.parent / "_internal"
    name = "_eab_task_" + hashlib.sha256(str(internal.resolve()).encode()).hexdigest()
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name,
            internal / "__init__.py",
            submodule_search_locations=[str(internal)],
        )
        if spec is None or spec.loader is None:
            raise ImportError("Cannot load task-local adapter")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(name + ".adapter")
