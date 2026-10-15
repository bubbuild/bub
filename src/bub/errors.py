"""Compatibility alias for :mod:`bub.core.errors`."""

import sys

from bub.core import errors as _module
from bub.core.errors import *  # noqa: F403

sys.modules[__name__] = _module
