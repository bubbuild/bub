"""Compatibility alias for :mod:`bubling.sidecars`."""

import sys

from bubling import sidecars as _module
from bubling.sidecars import *  # noqa: F403

sys.modules[__name__] = _module
