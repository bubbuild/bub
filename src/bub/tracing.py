"""Compatibility alias for :mod:`bubling.tracing`."""

import sys

from bubling import tracing as _module
from bubling.tracing import *  # noqa: F403

sys.modules[__name__] = _module
