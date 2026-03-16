"""Builtin CLI command adapter."""

# ruff: noqa: B008
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer
from republic.auth.openai_codex import CodexOAuthLoginError, OpenAICodexOAuthTokens, login_openai_codex_oauth

from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.framework import BubFramework

DEFAULT_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"


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
    channel.set_metadata(chat_id=chat_id, session_id=session_id, workspace=ctx.obj.workspace)  # type: ignore[attr-defined]
    asyncio.run(manager.listen_and_run())


def _prompt_for_codex_redirect(authorize_url: str) -> str:
    typer.echo("Open this URL in your browser and complete the Codex sign-in flow:\n")
    typer.echo(authorize_url)
    typer.echo("\nPaste the full callback URL or the authorization code.")
    return str(typer.prompt("callback")).strip()


def _resolve_codex_home(codex_home: Path | None) -> Path:
    if codex_home is not None:
        return codex_home.expanduser()
    return Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()


def _render_codex_login_result(tokens: OpenAICodexOAuthTokens, auth_path: Path) -> None:
    typer.echo("login: ok")
    typer.echo(f"account_id: {tokens.account_id or '-'}")
    typer.echo(f"auth_file: {auth_path}")
    typer.echo("usage: set BUB_MODEL=openai:gpt-5-codex and omit BUB_API_KEY")


def login(
    provider: str = typer.Argument(..., help="Authentication provider"),
    codex_home: Path | None = typer.Option(None, "--codex-home", help="Directory to store Codex OAuth credentials"),
    open_browser: bool = typer.Option(True, "--browser/--no-browser", help="Open the OAuth URL in a browser"),
    manual: bool = typer.Option(
        False,
        "--manual",
        help="Paste the callback URL or code instead of waiting for a local callback server",
    ),
    timeout_seconds: float = typer.Option(300.0, "--timeout", help="OAuth wait timeout in seconds"),
) -> None:
    """Authenticate with a provider and persist the resulting credentials."""

    if provider != "openai":
        typer.echo(f"Unsupported auth provider: {provider}", err=True)
        raise typer.Exit(1)

    resolved_codex_home = _resolve_codex_home(codex_home)
    prompt_for_redirect = _prompt_for_codex_redirect if manual or not open_browser else None

    try:
        tokens = login_openai_codex_oauth(
            codex_home=resolved_codex_home,
            prompt_for_redirect=prompt_for_redirect,
            open_browser=open_browser,
            redirect_uri=DEFAULT_CODEX_REDIRECT_URI,
            timeout_seconds=timeout_seconds,
        )
    except CodexOAuthLoginError as exc:
        typer.echo(f"Codex login failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    _render_codex_login_result(tokens, resolved_codex_home / "auth.json")
