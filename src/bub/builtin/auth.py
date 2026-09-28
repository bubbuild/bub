"""Bub's Codex login UX and explicit legacy-file migration; OAuth lives in Republic."""

# ruff: noqa: B008
from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import threading
import webbrowser
from base64 import urlsafe_b64decode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import typer
from republic.auth import codex
from republic.auth.codex import CodexAuthError, CodexTokens

DEFAULT_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
app = typer.Typer(name="login", help="Authentication related commands")


def resolve_codex_home(codex_home: str | Path | None = None) -> Path:
    return Path(codex_home if codex_home is not None else os.getenv("CODEX_HOME", "~/.codex")).expanduser()


def codex_token_path(codex_home: str | Path | None = None) -> Path:
    return resolve_codex_home(codex_home) / "bub-republic.json"


async def prepare_codex_tokens(codex_home: str | Path | None = None) -> CodexTokens:
    """Bub's pre-call refresh point. A failure never starts/replays inference."""
    path = codex_token_path(codex_home)
    tokens = codex.read_tokens(path)
    if tokens.is_expired(leeway=120):
        tokens = await codex.refresh_tokens(tokens)
        codex.write_tokens(path, tokens)
    return tokens


def _legacy_claims(access_token: Any) -> dict[str, Any]:
    """Only expiry/account hints for an explicit file import, never identity proof."""
    if not isinstance(access_token, str) or len(parts := access_token.split(".")) != 3:
        return {}
    try:
        value = json.loads(urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def migrate_codex_tokens(codex_home: str | Path | None = None) -> Path:
    """Explicitly import Bub/Codex auth.json into a separate Republic token file."""
    directory = resolve_codex_home(codex_home)
    destination = codex_token_path(directory)
    if destination.exists():
        raise CodexAuthError("migration_destination_exists")
    try:
        raw = json.loads((directory / "auth.json").read_text(encoding="utf-8"))
        old = raw["tokens"]
        hints = _legacy_claims(old.get("access_token"))
        expiry = old.get("expires_at", hints.get("exp"))
        if isinstance(expiry, str):
            timestamp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            expiry = timestamp.timestamp() if timestamp.tzinfo is not None else None
        account = old.get("account_id")
        if account is None and isinstance(route := hints.get("https://api.openai.com/auth"), dict):
            account = route.get("chatgpt_account_id")
        tokens = CodexTokens(old["access_token"], old["refresh_token"], expiry, account)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, CodexAuthError):
        failed = True
    else:
        failed = False
    if failed:
        raise CodexAuthError("invalid_legacy_credentials")
    codex.write_tokens(destination, tokens)
    return destination


@contextmanager
def _callback_receiver(redirect_uri: str) -> Iterator[Callable[[float], str | None]]:
    """Bind before opening a browser; only the local callback UX lives here."""
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.port is None:
        raise CodexAuthError("invalid_redirect")
    received: queue.Queue[str | None] = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            callback = urlsplit(self.path)
            if callback.path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            received.put(redirect_uri + "?" + callback.query)
            body = b"Callback received. Check your terminal for the login result."
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = ThreadingHTTPServer((parsed.hostname, parsed.port), Handler)
    except OSError:
        server = None
    if server is None:
        raise CodexAuthError("callback_bind_failed")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield lambda timeout: received.get(timeout=timeout)
    finally:
        received.put(None)  # Wake a cancelled to_thread receiver, too.
        server.shutdown()
        server.server_close()
        thread.join()


async def login_openai_codex_oauth(
    *,
    codex_home: str | Path | None = None,
    prompt_for_redirect: Callable[[str], str] | None = None,
    open_browser: bool = True,
    browser_opener: Callable[[str], Any] | None = None,
    redirect_uri: str = DEFAULT_CODEX_REDIRECT_URI,
    timeout_seconds: float = 300.0,
) -> CodexTokens:
    """Receive a full callback; Republic performs PKCE/state validation and exchange."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise CodexAuthError("invalid_timeout")
    authorization = await codex.create_authorization(redirect_uri=redirect_uri)
    opener = browser_opener or webbrowser.open
    callback: str | None
    if prompt_for_redirect is not None:
        if open_browser:
            opener(authorization.url)
        callback = prompt_for_redirect(authorization.url)
    else:
        with _callback_receiver(redirect_uri) as receive:
            if open_browser:
                opener(authorization.url)
            try:
                callback = await asyncio.to_thread(receive, timeout_seconds)
            except queue.Empty:
                callback = None
    if callback is None:
        raise CodexAuthError("callback_timeout")
    tokens = await codex.exchange_code(authorization, callback)
    path = codex_token_path(codex_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    codex.write_tokens(path, tokens)
    return tokens


def _prompt_for_codex_redirect(authorize_url: str) -> str:
    typer.echo("Open this URL in your browser and complete the Codex sign-in flow:\n")
    typer.echo(authorize_url)
    typer.echo("\nPaste the full callback URL, including state; a bare code is not accepted.")
    return str(typer.prompt("callback")).strip()


@app.command()
def openai(
    codex_home: Path | None = typer.Option(None, "--codex-home", help="Directory for Bub Codex credentials"),
    open_browser: bool = typer.Option(True, "--browser/--no-browser", help="Open the OAuth URL in a browser"),
    manual: bool = typer.Option(False, "--manual", help="Paste the full callback URL instead of a local server"),
    migrate: bool = typer.Option(False, "--migrate", help="Import existing auth.json locally; no login or network"),
    timeout_seconds: float = typer.Option(300.0, "--timeout", help="OAuth wait timeout in seconds"),
) -> None:
    """Log in with ChatGPT OAuth, or explicitly migrate an existing credential file."""
    directory = resolve_codex_home(codex_home)
    try:
        if migrate:
            migrate_codex_tokens(directory)
        else:
            asyncio.run(
                login_openai_codex_oauth(
                    codex_home=directory,
                    prompt_for_redirect=_prompt_for_codex_redirect if manual or not open_browser else None,
                    open_browser=open_browser,
                    timeout_seconds=timeout_seconds,
                )
            )
    except CodexAuthError as exc:
        typer.echo(f"Codex login failed: {exc.code}", err=True)
        raise typer.Exit(1) from None
    typer.echo("login: ok")
    typer.echo(f"auth_file: {codex_token_path(directory)}")
    typer.echo("usage: set BUB_MODEL=openai:<codex-model> and omit BUB_API_KEY and BUB_API_BASE")
