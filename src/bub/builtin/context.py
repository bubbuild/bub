"""Compatibility alias for :mod:`bub.core.context`."""

import sys

from bub.core import context as _module
from bub.core.context import *  # noqa: F403

sys.modules[__name__] = _module
