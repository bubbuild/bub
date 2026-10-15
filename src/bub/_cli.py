"""Entry point of the ``bub`` command, which needs the ``app`` extra."""

from __future__ import annotations

import re
import sys
from importlib.metadata import PackageNotFoundError, distribution, requires

_APP_REQUIREMENT = re.compile(r"""^([A-Za-z0-9][A-Za-z0-9._-]*).*;.*\bextra\s*==\s*["']app["']""")


def missing_app_requirements() -> list[str]:
    """Return the distributions required by ``bub[app]`` that are not installed."""
    try:
        requirements = requires("bub") or []
    except PackageNotFoundError:  # Running from a source tree that is not installed.
        return []
    missing: list[str] = []
    for requirement in requirements:
        if (match := _APP_REQUIREMENT.match(requirement)) is None:
            continue
        try:
            distribution(match.group(1))
        except PackageNotFoundError:
            missing.append(match.group(1))
    return missing


def main() -> None:
    if missing := missing_app_requirements():
        sys.exit(
            "The bub command needs the 'app' extra, which is not installed "
            f"(missing: {', '.join(missing)}).\n"
            "Install it with: uv tool install 'bub[app]'  or  pip install 'bub[app]'"
        )

    from bub.__main__ import app

    app()
