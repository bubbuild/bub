"""Bub framework CLI bootstrap."""

from __future__ import annotations

import sys

import typer

from bub.framework import BubFramework


def _instrument_bub() -> None:
    from loguru import logger

    from bub.builtin import telemetry

    logger.remove()
    logger.add(sys.stderr, colorize=True)

    tape_processor = telemetry.tape_span_processor()
    try:
        import logfire

        logfire.configure(additional_span_processors=[tape_processor])
        telemetry.mark_tape_span_processor_configured()
        logger.configure(handlers=[{"sink": sys.stderr, "colorize": True}, logfire.loguru_handler()])
    except Exception as exc:
        telemetry.configure_telemetry([tape_processor])
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
