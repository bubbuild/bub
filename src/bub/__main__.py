"""Bub framework CLI bootstrap."""

from __future__ import annotations

import os
import sys

import typer

from bub.framework import BubFramework


def _instrument_bub() -> None:
    from loguru import logger

    from bub.builtin import telemetry

    level = os.environ.get("BUB_LOG_LEVEL", "WARNING").upper()
    logger.remove()

    telemetry.configure_telemetry()
    logfire_handler = telemetry.loguru_handler()
    logfire_handler["level"] = "INFO"
    logger.configure(
        handlers=[
            {"sink": sys.stderr, "colorize": True, "level": level},
            logfire_handler,
        ]
    )


def create_cli_app() -> typer.Typer:
    _instrument_bub()
    framework = BubFramework()
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
