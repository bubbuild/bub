"""Compatibility alias for :mod:`bub.core.tape`."""

import sys

from bub.core import tape as _module
from bub.core.tape import *  # noqa: F403

sys.modules[__name__] = _module
