"""Builtin CLI command adapter."""

# ruff: noqa: B008
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import typer

from bub.builtin.auth import app as login_app  # noqa: F401
from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.framework import BubFramework


def run(
    ctx: typer.Context,
    message: str = typer.Argument(..., help="Inbound message content"),
    channel: str = typer.Option("cli", "--channel", help="Message channel"),
    chat_id: str = typer.Option("local", "--chat-id", help="Chat id"),
    sender_id: str = typer.Option("human", "--sender-id", help="Sender id"),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id"),
) -> None:
    """Run one inbound message through the framework pipeline."""

    framework = ctx.ensure_object(BubFramework)
    inbound = ChannelMessage(
        session_id=f"{channel}:{chat_id}" if session_id is None else session_id,
        content=message,
        channel=channel,
        chat_id=chat_id,
        context={"sender_id": sender_id},
    )

    result = asyncio.run(framework.process_inbound(inbound))
    for outbound in result.outbounds:
        rendered = str(field_of(outbound, "content", ""))
        target_channel = str(field_of(outbound, "channel", "stdout"))
        target_chat = str(field_of(outbound, "chat_id", "local"))
        typer.echo(f"[{target_channel}:{target_chat}]\n{rendered}")


def list_hooks(ctx: typer.Context) -> None:
    """Show hook implementation mapping."""
    framework = ctx.ensure_object(BubFramework)
    report = framework.hook_report()
    if not report:
        typer.echo("(no hook implementations)")
        return
    for hook_name, adapter_names in report.items():
        typer.echo(f"{hook_name}: {', '.join(adapter_names)}")


def gateway(
    ctx: typer.Context,
    enable_channels: list[str] = typer.Option([], "--enable-channel", help="Channels to enable for CLI (default: all)"),
) -> None:
    """Start message listeners(like telegram)."""
    from bub.channels.manager import ChannelManager

    framework = ctx.ensure_object(BubFramework)

    manager = ChannelManager(framework, enabled_channels=enable_channels or None)
    asyncio.run(manager.listen_and_run())


def chat(
    ctx: typer.Context,
    chat_id: str = typer.Option("local", "--chat-id", help="Chat id"),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional session id"),
) -> None:
    """Start a REPL chat session."""
    from bub.channels.manager import ChannelManager

    framework = ctx.ensure_object(BubFramework)

    manager = ChannelManager(framework, enabled_channels=["cli"])
    channel = manager.get_channel("cli")
    if channel is None:
        typer.echo("CLI channel not found. Please check your hook implementations.")
        raise typer.Exit(1)
    channel.set_metadata(chat_id=chat_id, session_id=session_id)  # type: ignore[attr-defined]
    asyncio.run(manager.listen_and_run())


@lru_cache(maxsize=1)
def _find_uv() -> str:
    import shutil
    import sysconfig

    bin_path = sysconfig.get_path("scripts")
    uv_path = shutil.which("uv", path=os.pathsep.join([bin_path, os.getenv("PATH", "")]))
    if uv_path is None:
        raise FileNotFoundError("uv executable not found in PATH or scripts directory.")
    return uv_path


def _is_in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _uv(*args: str) -> subprocess.CompletedProcess:
    uv_executable = _find_uv()
    if not _is_in_venv():
        typer.secho("Please install Bub in a virtual environment to use this command.", err=True, fg="red")
        raise typer.Exit(1)
    env = {**os.environ, "VIRTUAL_ENV": sys.prefix}
    try:
        return subprocess.run([uv_executable, *args], env=env, check=True)
    except subprocess.CalledProcessError as e:
        typer.secho(f"Command 'uv {' '.join(args)}' failed with exit code {e.returncode}.", err=True, fg="red")
        raise typer.Exit(e.returncode) from e


BUB_CONTRIB_REPO = "https://github.com/bubbuild/bub-contrib.git"


def _build_requirement(spec: str) -> str:
    if spec.startswith(("git@", "https://")):
        # Git URL
        return f"git+{spec}"
    elif "/" in spec:
        # owner/repo format
        repo, *rest = spec.partition("@")
        ref = "".join(rest)
        return f"git+https://github.com/{repo}.git{ref}"
    else:
        # Assume it's a package name in bub-contrib
        name, *rest = spec.partition("@")
        ref = "".join(rest)
        return f"git+{BUB_CONTRIB_REPO}{ref}#subdirectory=packages/{name}"


def _ensure_project() -> None:
    if (Path.cwd() / "pyproject.toml").is_file():
        return
    _uv("init", "--bare", "--name", "bub-project", "--app")


def install(
    spec: str = typer.Argument(
        ..., help="Package specification to install, can be a git URL, owner/repo, or package name in bub-contrib."
    ),
) -> None:
    """Install a plugin into Bub's environment."""
    _ensure_project()
    req = _build_requirement(spec)
    _uv("add", "--active", req)


def uninstall(
    package: str = typer.Argument(..., help="Package name to uninstall (must match the name in pyproject.toml)"),
) -> None:
    """Uninstall a plugin from Bub's environment."""
    _ensure_project()
    _uv("remove", "--active", "--no-sync", package)
    _uv("sync", "--active", "--frozen", "--inexact")


def update(
    package: str | None = typer.Argument(
        None, help="Optional package name to update (must match the name in pyproject.toml)"
    ),
) -> None:
    """Update selected package or all packages in Bub's environment."""
    _ensure_project()
    if package is None:
        _uv("sync", "--active", "--upgrade", "--inexact")
    else:
        _uv("sync", "--active", "--inexact", "--upgrade-package", package)
