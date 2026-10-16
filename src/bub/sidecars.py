"""Compatibility alias for :mod:`bub.core.sidecars`."""

import sys

from bub.core import sidecars as _module
from bub.core.sidecars import *  # noqa: F403

sys.modules[__name__] = _module
