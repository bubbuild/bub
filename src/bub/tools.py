"""Compatibility alias for :mod:`bub.agent.tools`."""

import sys

from bub.agent import tools as _module
from bub.agent.tools import *  # noqa: F403

sys.modules[__name__] = _module
