"""Bub framework package."""

from __future__ import annotations

import os
from importlib import import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version
from pathlib import Path
from typing import TYPE_CHECKING

from bub.configure import Settings, config, ensure_config
from bub.framework import DEFAULT_HOME, BubFramework
from bub.hookspecs import hookimpl
from bub.tools import tool

__all__ = ["BubFramework", "Settings", "config", "ensure_config", "home", "hookimpl", "tool"]

try:
    __version__ = import_module("bub._version").version
except ModuleNotFoundError:
    try:
        __version__ = metadata_version("bub")
    except PackageNotFoundError:
        __version__ = "0.0.0"


if TYPE_CHECKING:
    home: Path


def __getattr__(name: str):
    if name == "home":
        if "BUB_HOME" in os.environ:
            return Path(os.environ["BUB_HOME"])
        return DEFAULT_HOME
    raise AttributeError(f"module {__name__} has no attribute {name}")
