"""Bub framework package.

The top-level names are imported on first access, so ``import bub.core`` does not load
the framework, its settings or the CLI.
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
    from bub.core.tools import tool
    from bub.framework import BubFramework
    from bub.hooks import hookimpl

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
    "tool": "bub.core.tools",
}

try:
    __version__ = import_module("bub._version").version
except ModuleNotFoundError:
    try:
        __version__ = metadata_version("bub")
    except PackageNotFoundError:
        __version__ = "0.0.0"


def __getattr__(name: str) -> Any:
    if name == "home":
        # Importing bub.configure loads ``.env``, which may set ``BUB_HOME``.
        from bub.configure import DEFAULT_HOME

        if "BUB_HOME" in os.environ:
            return Path(os.path.expanduser(os.environ["BUB_HOME"]))
        return DEFAULT_HOME
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__} has no attribute {name}")
    return getattr(import_module(_EXPORTS[name]), name)
