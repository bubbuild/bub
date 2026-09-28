"""Bub's Codex login UX and credential policy; OAuth lives in Republic."""

# ruff: noqa: B008
from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import tempfile
import threading
import time
import webbrowser
from base64 import urlsafe_b64decode
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
    return resolve_codex_home(codex_home) / "auth.json"


def _claims(access_token: Any) -> dict[str, Any]:
    """Unverified account/expiry hints, not an identity or authorization check."""
    if not isinstance(access_token, str) or len(parts := access_token.split(".")) != 3:
        return {}
    try:
        value = json.loads(urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def codex_account_id(access_token: str) -> str | None:
    hints = _claims(access_token).get("https://api.openai.com/auth")
    account = hints.get("chatgpt_account_id") if isinstance(hints, dict) else None
    return account.strip() if isinstance(account, str) and account.strip() else None


def _timestamp(value: Any) -> float | None:
    if type(value) in (int, float):
        return float(value) if 0 < value <= 1e308 and math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _read_auth(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raise CodexAuthError("invalid_credentials_file")
    return raw


def _tokens(raw: dict[str, Any]) -> CodexTokens:
    nested = raw.get("tokens")
    if not isinstance(nested, dict) or not nested.get("refresh_token"):
        raise CodexAuthError("invalid_credentials_file")
    access = nested.get("access_token")
    refresh = nested.get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise CodexAuthError("invalid_credentials_file")
    access, refresh = access.strip(), refresh.strip()
    expiry = _timestamp(nested.get("expires_at")) or _timestamp(_claims(access).get("exp"))
    if expiry is None:
        # Preserve Bub's pre-SDK freshness policy. Republic never invents expiry.
        expiry = (_timestamp(raw.get("last_refresh")) or time.time()) + 3600
    account = nested.get("account_id") or codex_account_id(access)
    return CodexTokens(access, refresh, expiry, account)


def load_codex_tokens(codex_home: str | Path | None = None) -> CodexTokens | None:
    """Select only parseable existing auth.json credentials; reading never writes."""
    try:
        return _tokens(_read_auth(codex_token_path(codex_home)))
    except CodexAuthError:
        return None


def save_codex_tokens(path: Path, tokens: CodexTokens) -> None:
    """Update the original nested format atomically, retaining unrelated fields."""
    try:
        raw = _read_auth(path) if path.exists() else {}
    except CodexAuthError:
        raw = {}  # A successful new login can replace an unreadable old layout.
    nested = raw.get("tokens", {})
    if not isinstance(nested, dict):
        nested = {}
    nested.update(access_token=tokens.access_token)
    if tokens.refresh_token is not None:
        nested["refresh_token"] = tokens.refresh_token
    if tokens.expires_at is not None:
        nested["expires_at"] = datetime.fromtimestamp(tokens.expires_at, UTC).isoformat().replace("+00:00", "Z")
    else:
        nested.pop("expires_at", None)
    if tokens.account_id is not None:
        nested["account_id"] = tokens.account_id
    raw.update(tokens=nested, last_refresh=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    _write_auth(path, raw)


def _write_auth(path: Path, raw: dict[str, Any]) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(raw, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        failed = True
    else:
        failed = False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                failed = True
    if failed:
        raise CodexAuthError("credential_write_failed")


async def prepare_codex_tokens(codex_home: str | Path | None = None) -> CodexTokens:
    """Bub refreshes 120 seconds early; a failed refresh may use an unexpired token."""
    path = codex_token_path(codex_home)
    tokens = _tokens(_read_auth(path))
    if not tokens.is_expired(leeway=120):
        return tokens
    try:
        updated = await codex.refresh_tokens(tokens)
    except CodexAuthError:
        if tokens.is_expired():
            raise
        return tokens
    # Persistence failures stop inference, including after refresh-token rotation.
    save_codex_tokens(path, updated)
    return updated


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
    """Receive a callback URL or manual code; Republic performs OAuth/PKCE exchange."""
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
    callback = callback.strip()
    if "://" in callback:
        tokens = await codex.exchange_code(authorization, callback)
    elif "code=" in callback or "error=" in callback:
        tokens = await codex.exchange_code(authorization, redirect_uri + "?" + callback.lstrip("?"))
    else:
        tokens = await codex.exchange_authorization_code(authorization, callback)
    save_codex_tokens(codex_token_path(codex_home), tokens)
    return tokens


def _prompt_for_codex_redirect(authorize_url: str) -> str:
    typer.echo("Open this URL in your browser and complete the Codex sign-in flow:\n")
    typer.echo(authorize_url)
    typer.echo("\nPaste the callback URL or authorization code.")
    return str(typer.prompt("callback")).strip()


@app.command()
def openai(
    codex_home: Path | None = typer.Option(None, "--codex-home", help="Directory for Bub Codex credentials"),
    open_browser: bool = typer.Option(True, "--browser/--no-browser", help="Open the OAuth URL in a browser"),
    manual: bool = typer.Option(False, "--manual", help="Paste a callback URL or code instead of a local server"),
    timeout_seconds: float = typer.Option(300.0, "--timeout", help="OAuth wait timeout in seconds"),
) -> None:
    """Log in with ChatGPT OAuth and save the existing Codex auth.json format."""
    directory = resolve_codex_home(codex_home)
    try:
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
