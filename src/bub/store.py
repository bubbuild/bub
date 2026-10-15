"""Compatibility alias for :mod:`bub.agent.store`."""

import sys

from bub.agent import store as _module
from bub.agent.store import *  # noqa: F403

sys.modules[__name__] = _module
