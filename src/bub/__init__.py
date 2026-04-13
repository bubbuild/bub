"""Bub framework package."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version

from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.tools import tool

__all__ = ["BubFramework", "hookimpl", "tool"]

try:
    __version__ = import_module("bub._version").version
except ModuleNotFoundError:
    try:
        __version__ = metadata_version("bub")
    except PackageNotFoundError:
        __version__ = "0.0.0"
