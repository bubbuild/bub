"""Bub framework CLI bootstrap."""

from __future__ import annotations

import sys

import typer

from bub.framework import BubFramework


def _instrument_bub() -> None:
    from loguru import logger

    try:
        import logfire

        logfire.configure()
    except ImportError:
        logger.remove()
        logger.add(sys.stderr, colorize=True)
    else:
        logger.remove()
        logger.add(sys.stderr, colorize=True)
        logger.add(logfire.loguru_handler())


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
