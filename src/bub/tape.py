"""Compatibility alias for :mod:`bub.agent.tape`."""

import sys

from bub.agent import tape as _module
from bub.agent.tape import *  # noqa: F403

sys.modules[__name__] = _module
