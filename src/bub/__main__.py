"""Bub framework CLI bootstrap."""

from __future__ import annotations

import sys

import typer

from bub.framework import BubFramework


def _instrument_bub() -> None:
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, colorize=True)

    try:
        import logfire

        from bub.builtin.telemetry import tape_span_processor

        logfire.configure(additional_span_processors=[tape_span_processor()])
        logger.configure(handlers=[{"sink": sys.stderr, "colorize": True}, logfire.loguru_handler()])
    except Exception as exc:
        logger.debug("logfire instrumentation disabled: {}", exc)


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
