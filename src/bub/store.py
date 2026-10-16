"""Compatibility alias for :mod:`bub.core.store`."""

import sys

from bub.core import store as _module
from bub.core.store import *  # noqa: F403

sys.modules[__name__] = _module
