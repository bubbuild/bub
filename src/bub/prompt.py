"""Compatibility alias for :mod:`bub.core.prompt`."""

import sys

from bub.core import prompt as _module
from bub.core.prompt import *  # noqa: F403

sys.modules[__name__] = _module
