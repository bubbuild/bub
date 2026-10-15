"""Compatibility alias for :mod:`bubling.store`."""

import sys

from bubling import store as _module
from bubling.store import *  # noqa: F403

sys.modules[__name__] = _module
