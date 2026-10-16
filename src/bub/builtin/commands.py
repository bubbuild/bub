"""Compatibility alias for :mod:`bub.core.commands`."""

import sys

from bub.core import commands as _module
from bub.core.commands import *  # noqa: F403

sys.modules[__name__] = _module
