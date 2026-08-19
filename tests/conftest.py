from __future__ import annotations

import sys
import types
from pathlib import Path

PACKAGE_NAME = "hearthstone_companion_under_test"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# Pytest treats the plugin root as a package because it contains __init__.py.
# Stub that collection-only import so the unavailable NEKO SDK entrypoint is
# not executed; production modules are loaded through PACKAGE_NAME below.
if "__init__" not in sys.modules:
    root_package = types.ModuleType("__init__")
    root_package.__file__ = str(PROJECT_ROOT / "__init__.py")
    root_package.__package__ = ""
    root_package.__path__ = [str(PROJECT_ROOT)]
    sys.modules["__init__"] = root_package


# The plugin directory contains dots and is loaded by N.E.K.O rather than by
# Python's normal package discovery. Register a lightweight test-only package
# so relative imports work without executing the SDK-dependent plugin entrypoint.
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__file__ = str(PROJECT_ROOT / "__init__.py")
    package.__package__ = PACKAGE_NAME
    package.__path__ = [str(PROJECT_ROOT)]
    sys.modules[PACKAGE_NAME] = package
