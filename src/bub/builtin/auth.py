"""Authentication helpers for builtin providers."""

# ruff: noqa: B008
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import typer
from authlib.integrations.httpx_client import OAuth2Client

CODEX_PROVIDER = "openai"
DEFAULT_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"

_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"  # noqa: S105
_CODEX_OAUTH_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
_CODEX_OAUTH_SCOPE = "openid profile email offline_access"
_CODEX_OAUTH_ORIGINATOR = "codex_cli_rs"

app = typer.Typer(name="login", help="Authentication related commands")


class CodexOAuthLoginError(RuntimeError):
    """Raised when Codex OAuth login cannot complete."""


class CodexOAuthStateMismatchError(CodexOAuthLoginError):
    """Raised when OAuth state validation fails."""


class CodexOAuthMissingCodeError(CodexOAuthLoginError):
    """Raised when OAuth redirect does not include an authorization code."""


class CodexOAuthResponseError(TypeError):
    """Raised when the OAuth token endpoint returns a malformed payload."""


@dataclass(frozen=True)
class OpenAICodexOAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: int
    account_id: str | None = None


def resolve_codex_home(codex_home: str | Path | None = None) -> Path:
    if codex_home is not None:
        return Path(codex_home).expanduser()
    return Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()


def resolve_codex_auth_path(codex_home: str | Path | None = None) -> Path:
    return resolve_codex_home(codex_home) / "auth.json"


def load_openai_codex_oauth_tokens(codex_home: str | Path | None = None) -> OpenAICodexOAuthTokens | None:
    auth_path = resolve_codex_auth_path(codex_home)
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _parse_tokens(payload)


def save_openai_codex_oauth_tokens(
    tokens: OpenAICodexOAuthTokens,
    codex_home: str | Path | None = None,
) -> Path:
    auth_path = resolve_codex_auth_path(codex_home)
    auth_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        existing = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    payload: dict[str, Any] = existing if isinstance(existing, dict) else {}

    token_payload = payload.get("tokens")
    if not isinstance(token_payload, dict):
        token_payload = {}
    token_payload.update({
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": _unix_to_rfc3339(tokens.expires_at),
    })
    if tokens.account_id:
        token_payload["account_id"] = tokens.account_id
    payload["tokens"] = token_payload
    payload["last_refresh"] = _unix_to_rfc3339(int(time.time()))

    auth_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        os.chmod(auth_path, 0o600)
    return auth_path


def refresh_openai_codex_oauth_tokens(
    refresh_token: str,
    *,
    timeout_seconds: float = 15.0,
    client_id: str = _CODEX_OAUTH_CLIENT_ID,
    token_url: str = _CODEX_OAUTH_TOKEN_URL,
) -> OpenAICodexOAuthTokens:
    with OAuth2Client(client_id=client_id, timeout=timeout_seconds, trust_env=True) as oauth:
        payload = oauth.refresh_token(url=token_url, refresh_token=refresh_token)
    return _tokens_from_token_payload(payload, account_id=None)


def openai_codex_oauth_resolver(
    codex_home: str | Path | None = None,
    *,
    refresh_skew_seconds: int = 120,
    refresh_timeout_seconds: float = 15.0,
    refresher: Callable[[str], OpenAICodexOAuthTokens] | None = None,
) -> Callable[[str], str | None]:
    """Build a provider-scoped OAuth token resolver with refresh support."""

    lock = threading.Lock()
    if refresher is None:
        refresher = lambda refresh_token: refresh_openai_codex_oauth_tokens(
            refresh_token,
            timeout_seconds=refresh_timeout_seconds,
        )

    def _resolve(provider: str) -> str | None:
        if provider != CODEX_PROVIDER:
            return None
        with lock:
            tokens = load_openai_codex_oauth_tokens(codex_home)
            if tokens is None:
                return None

            now = int(time.time())
            if tokens.expires_at > now + refresh_skew_seconds:
                return tokens.access_token

            try:
                refreshed = refresher(tokens.refresh_token)
            except Exception:
                return tokens.access_token if tokens.expires_at > now else None

            persisted = OpenAICodexOAuthTokens(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token,
                expires_at=refreshed.expires_at,
                account_id=refreshed.account_id or tokens.account_id,
            )
            save_openai_codex_oauth_tokens(persisted, codex_home)
            return persisted.access_token

    return _resolve


def login_openai_codex_oauth(
    *,
    codex_home: str | Path | None = None,
    prompt_for_redirect: Callable[[str], str] | None = None,
    open_browser: bool = True,
    browser_opener: Callable[[str], Any] | None = None,
    redirect_uri: str = DEFAULT_CODEX_REDIRECT_URI,
    timeout_seconds: float = 300.0,
    client_id: str = _CODEX_OAUTH_CLIENT_ID,
    authorize_url: str = _CODEX_OAUTH_AUTHORIZE_URL,
    token_url: str = _CODEX_OAUTH_TOKEN_URL,
    scope: str = _CODEX_OAUTH_SCOPE,
    originator: str = _CODEX_OAUTH_ORIGINATOR,
) -> OpenAICodexOAuthTokens:
    """Run the OpenAI Codex OAuth flow and persist tokens."""

    verifier = _build_pkce_verifier()
    state = secrets.token_hex(16)
    oauth_url = _build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=verifier,
        state=state,
        authorize_url=authorize_url,
        scope=scope,
        originator=originator,
    )

    if open_browser:
        opener = browser_opener or webbrowser.open
        opener(oauth_url)

    if prompt_for_redirect is None:
        callback_values = _wait_for_local_oauth_callback(redirect_uri=redirect_uri, timeout_seconds=timeout_seconds)
        if callback_values is None:
            raise CodexOAuthLoginError(
                "Did not receive OAuth callback. "
                f"redirect_uri={redirect_uri!r}, timeout_seconds={timeout_seconds}. "
                "Try increasing --timeout or use --manual."
            )
        code, returned_state = callback_values
    else:
        code, returned_state = _extract_code_and_state(prompt_for_redirect(oauth_url))

    if returned_state and returned_state != state:
        raise CodexOAuthStateMismatchError
    if not isinstance(code, str) or not code.strip():
        raise CodexOAuthMissingCodeError

    tokens = _exchange_openai_codex_authorization_code(
        code=code.strip(),
        verifier=verifier,
        redirect_uri=redirect_uri,
        timeout_seconds=timeout_seconds,
        client_id=client_id,
        token_url=token_url,
    )
    save_openai_codex_oauth_tokens(tokens, codex_home)
    return tokens


def _prompt_for_codex_redirect(authorize_url: str) -> str:
    typer.echo("Open this URL in your browser and complete the Codex sign-in flow:\n")
    typer.echo(authorize_url)
    typer.echo("\nPaste the full callback URL or the authorization code.")
    return str(typer.prompt("callback")).strip()


def _render_codex_login_result(tokens: OpenAICodexOAuthTokens, auth_path: Path) -> None:
    typer.echo("login: ok")
    typer.echo(f"account_id: {tokens.account_id or '-'}")
    typer.echo(f"auth_file: {auth_path}")
    typer.echo("usage: set BUB_MODEL=openai:<codex-model> and omit BUB_API_KEY")


@app.command()
def openai(
    codex_home: Path | None = typer.Option(None, "--codex-home", help="Directory to store Codex OAuth credentials"),
    open_browser: bool = typer.Option(True, "--browser/--no-browser", help="Open the OAuth URL in a browser"),
    manual: bool = typer.Option(
        False,
        "--manual",
        help="Paste the callback URL or code instead of waiting for a local callback server",
    ),
    timeout_seconds: float = typer.Option(300.0, "--timeout", help="OAuth wait timeout in seconds"),
) -> None:
    """Login with OpenAI OAuth."""

    resolved_codex_home = resolve_codex_home(codex_home)
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


def extract_openai_codex_account_id(access_token: str) -> str | None:
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        payload = json.loads(urlsafe_b64decode((payload_segment + padding).encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    if not isinstance(account_id, str):
        return None
    return account_id.strip() or None


def _parse_tokens(payload: dict[str, Any]) -> OpenAICodexOAuthTokens | None:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        return None
    access = access_token.strip()
    refresh = refresh_token.strip()
    if not access or not refresh:
        return None

    expires_at = _parse_expiry(tokens.get("expires_at"), payload.get("last_refresh"))
    account_id = tokens.get("account_id")
    return OpenAICodexOAuthTokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        account_id=account_id if isinstance(account_id, str) else None,
    )


def _parse_expiry(expires_raw: object, last_refresh_raw: object) -> int:
    if isinstance(expires_raw, int | float):
        return int(expires_raw)
    if isinstance(expires_raw, str):
        return _rfc3339_to_unix(expires_raw)
    if isinstance(last_refresh_raw, int | float):
        return int(last_refresh_raw) + 3600
    if isinstance(last_refresh_raw, str):
        return _rfc3339_to_unix(last_refresh_raw) + 3600
    return int(time.time()) + 3600


def _tokens_from_token_payload(payload: dict[str, Any], *, account_id: str | None) -> OpenAICodexOAuthTokens:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise CodexOAuthResponseError
    if not isinstance(expires_in, int | float):
        raise CodexOAuthResponseError

    access = access_token.strip()
    return OpenAICodexOAuthTokens(
        access_token=access,
        refresh_token=refresh_token.strip(),
        expires_at=int(time.time() + float(expires_in)),
        account_id=account_id or extract_openai_codex_account_id(access),
    )


def _exchange_openai_codex_authorization_code(
    code: str,
    *,
    verifier: str,
    redirect_uri: str,
    timeout_seconds: float,
    client_id: str,
    token_url: str,
) -> OpenAICodexOAuthTokens:
    with OAuth2Client(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge_method="S256",
        timeout=timeout_seconds,
    ) as oauth:
        payload = oauth.fetch_token(
            url=token_url,
            grant_type="authorization_code",
            code=code,
            code_verifier=verifier,
        )
    return _tokens_from_token_payload(
        payload,
        account_id=extract_openai_codex_account_id(str(payload.get("access_token", ""))),
    )


def _build_pkce_verifier() -> str:
    return urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    authorize_url: str,
    scope: str,
    originator: str,
) -> str:
    with OAuth2Client(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge_method="S256",
        trust_env=True,
    ) as oauth:
        url, _ = oauth.create_authorization_url(
            authorize_url,
            state=state,
            code_verifier=code_challenge,
            id_token_add_organizations="true",  # noqa: S106
            codex_cli_simplified_flow="true",
            originator=originator,
        )
    return str(url)


def _extract_code_and_state(input_value: str) -> tuple[str | None, str | None]:
    raw = input_value.strip()
    if not raw:
        return None, None

    parsed = urllib.parse.urlsplit(raw)
    query = urllib.parse.parse_qs(parsed.query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    if isinstance(code, str) or isinstance(state, str):
        return code if isinstance(code, str) else None, state if isinstance(state, str) else None

    if "code=" in raw:
        parsed_query = urllib.parse.parse_qs(raw)
        code = parsed_query.get("code", [None])[0]
        state = parsed_query.get("state", [None])[0]
        return code if isinstance(code, str) else None, state if isinstance(state, str) else None

    return raw, None


def _wait_for_local_oauth_callback(
    *, redirect_uri: str, timeout_seconds: float
) -> tuple[str | None, str | None] | None:
    parsed_redirect = urllib.parse.urlsplit(redirect_uri)
    if parsed_redirect.scheme != "http" or (parsed_redirect.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        return None
    if parsed_redirect.port is None:
        return None

    path = parsed_redirect.path or "/"
    state: dict[str, str | None] = {"code": None, "state": None}
    done = threading.Event()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return

            query = urllib.parse.parse_qs(parsed.query)
            with lock:
                code = query.get("code", [None])[0]
                returned_state = query.get("state", [None])[0]
                state["code"] = code if isinstance(code, str) else None
                state["state"] = returned_state if isinstance(returned_state, str) else None
            done.set()

            body = (
                b"<!doctype html><html><body><p>Authentication successful. Return to your terminal.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = ThreadingHTTPServer((parsed_redirect.hostname or "localhost", parsed_redirect.port), Handler)
    except OSError:
        return None

    server.timeout = 0.2
    deadline = time.monotonic() + timeout_seconds
    try:
        while not done.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not done.is_set():
        return None
    with lock:
        return state["code"], state["state"]


def _unix_to_rfc3339(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rfc3339_to_unix(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return int(time.time())
