"""Compatibility alias for :mod:`bubling.errors`."""

import sys

from bubling import errors as _module
from bubling.errors import *  # noqa: F403

sys.modules[__name__] = _module
