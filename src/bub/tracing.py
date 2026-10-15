"""Compatibility alias for :mod:`bub.core.tracing`."""

import sys

from bub.core import tracing as _module
from bub.core.tracing import *  # noqa: F403

sys.modules[__name__] = _module
