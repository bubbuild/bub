"""Compatibility alias for :mod:`bubling.tape`."""

import sys

from bubling import tape as _module
from bubling.tape import *  # noqa: F403

sys.modules[__name__] = _module
