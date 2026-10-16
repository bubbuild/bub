"""Compatibility alias for :mod:`bub.core.tools`."""

import sys

from bub.core import tools as _module
from bub.core.tools import *  # noqa: F403

sys.modules[__name__] = _module
