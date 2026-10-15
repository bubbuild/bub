"""Compatibility alias for :mod:`bubling.tools`."""

import sys

from bubling import tools as _module
from bubling.tools import *  # noqa: F403

sys.modules[__name__] = _module
