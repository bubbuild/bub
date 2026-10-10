"""CLI entry points for Republic-managed account authentication."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from republic.errors import AuthenticationError
from republic.providers import CodexAuth

app = typer.Typer(name="login", help="Account authentication")


@app.callback()
def login() -> None:
    """Authenticate a provider account."""


@app.command("codex")
def login_codex(
    executable: Annotated[str, typer.Option("--executable", help="Codex CLI executable")] = "codex",
    device_auth: Annotated[bool, typer.Option("--device-auth", help="Use device authorization")] = False,
    codex_home: Annotated[Path | None, typer.Option("--codex-home", help="Codex credential directory")] = None,
) -> None:
    """Log in with the Codex CLI using file credential storage."""
    try:
        if codex_home is None:
            auth = asyncio.run(CodexAuth.login(executable=executable, device_auth=device_auth))
        else:
            home = codex_home.expanduser().resolve()
            command = [executable, "login", "--config", 'cli_auth_credentials_store="file"']
            if device_auth:
                command.append("--device-auth")
            try:
                subprocess.run(command, env={**os.environ, "CODEX_HOME": str(home)}, check=True)
            except OSError:
                raise AuthenticationError(f"Cannot start {executable}; install the Codex CLI.") from None
            except subprocess.CalledProcessError as exc:
                raise AuthenticationError(f"Codex login failed (exit {exc.returncode})") from None
            auth = CodexAuth.from_file(home / "auth.json")
    except AuthenticationError as exc:
        typer.secho(f"login: failed: {exc}", fg="red", err=True)
        raise typer.Exit(1) from None
    typer.echo("login: ok")
    typer.echo(f"account_id: {auth.account_id}")
    typer.echo("Use BUB_MODEL=codex:<model> for ChatGPT plan access.")
