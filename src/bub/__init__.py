"""Bub framework package.

The top-level names are imported on first access, so importing a submodule such as
``bub.envelope`` does not load the framework, its settings or the CLI.
"""

from __future__ import annotations

import os
from importlib import import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bub.configure import Settings, config, ensure_config
    from bub.framework import BubFramework
    from bub.hooks import hookimpl
    from bubling.tools import tool

    home: Path

__all__ = [
    "BubFramework",
    "Settings",
    "config",
    "ensure_config",
    "home",
    "hookimpl",
    "tool",
]

_EXPORTS = {
    "BubFramework": "bub.framework",
    "Settings": "bub.configure",
    "config": "bub.configure",
    "ensure_config": "bub.configure",
    "hookimpl": "bub.hooks",
}

try:
    __version__ = import_module("bub._version").version
except ModuleNotFoundError:
    try:
        __version__ = metadata_version("bub")
    except PackageNotFoundError:
        __version__ = "0.0.0"


def __getattr__(name: str) -> Any:
    if name == "tool":
        from bubling.tools import tool

        return tool
    if name != "home" and name not in _EXPORTS:
        raise AttributeError(f"module {__name__} has no attribute {name}")
    # The framework loads ``.env`` on import, before settings, plugins or ``home`` read the environment.
    framework = import_module("bub.framework")
    if name == "home":
        if "BUB_HOME" in os.environ:
            return Path(os.path.expanduser(os.environ["BUB_HOME"]))
        return framework.DEFAULT_HOME
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value
