"""Bub framework CLI bootstrap."""

from __future__ import annotations

import os
import sys

import typer

from bub.builtin.settings import load_settings
from bub.framework import BubFramework

LOG_LEVEL = {
    0: "INFO",
    1: "DEBUG",
    2: "TRACE",
}


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
    framework = BubFramework()
    _instrument_bub(LOG_LEVEL[load_settings().verbose])
    framework.load_hooks()
    app = framework.create_cli_app()

    if not app.registered_commands:

        @app.command("help")
        def _help() -> None:
            typer.echo("No CLI command loaded.")

    return app


app = create_cli_app()

if __name__ == "__main__":
    app()
