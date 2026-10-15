"""Compatibility alias for :mod:`bub.core.streaming`."""

import sys

from bub.core import streaming as _module
from bub.core.streaming import *  # noqa: F403

sys.modules[__name__] = _module
