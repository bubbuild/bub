"""Bub framework CLI bootstrap.

The CLI needs the ``app`` extra, so this module imports it only when the command runs:
``main`` reports missing requirements instead of failing on an import.
"""

from __future__ import annotations

import os
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution, requires
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer

LOG_LEVEL = {
    0: "INFO",
    1: "DEBUG",
    2: "TRACE",
}

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


def _instrument_bub(level: str) -> None:
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level=level, colorize=True, diagnose=False)

    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from bub.tracing import configure_otlp

        try:
            configure_otlp()
        except Exception as exc:
            logger.warning("OTLP instrumentation disabled: {}", exc)
        return

    try:
        import logfire
        from logfire.integrations.loguru import LogfireHandler

        logfire.configure()
        logger.add(LogfireHandler(), level=level, format="{message}", diagnose=False)
    except Exception as exc:
        logger.debug("logfire instrumentation disabled: {}", exc)


def create_cli_app() -> typer.Typer:
    import typer

    from bub.builtin.settings import load_settings
    from bub.framework import BubFramework

    framework = BubFramework()
    _instrument_bub(LOG_LEVEL[load_settings().verbose])
    framework.load_hooks()
    app = framework.create_cli_app()

    if not app.registered_commands:

        @app.command("help")
        def _help() -> None:
            typer.echo("No CLI command loaded.")

    return app


def main() -> None:
    """Run the ``bub`` command."""
    if missing := missing_app_requirements():
        sys.exit(
            "The bub command needs the 'app' extra, which is not installed "
            f"(missing: {', '.join(missing)}).\n"
            "Install it with: uv tool install 'bub[app]'  or  pip install 'bub[app]'"
        )
    create_cli_app()()


if __name__ == "__main__":
    main()
