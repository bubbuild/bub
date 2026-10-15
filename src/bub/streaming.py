"""Compatibility alias for :mod:`bubling.streaming`."""

import sys

from bubling import streaming as _module
from bubling.streaming import *  # noqa: F403

sys.modules[__name__] = _module
