"""Compatibility alias for :mod:`bub.agent.sidecars`."""

import sys

from bub.agent import sidecars as _module
from bub.agent.sidecars import *  # noqa: F403

sys.modules[__name__] = _module
