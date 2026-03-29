import os
from pathlib import Path

import typer
from republic.auth.openai_codex import CodexOAuthLoginError, OpenAICodexOAuthTokens, login_openai_codex_oauth

DEFAULT_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
app = typer.Typer(name="login", help="Authentication related commands")


def _render_codex_login_result(tokens: OpenAICodexOAuthTokens, auth_path: Path) -> None:
    typer.echo("login: ok")
    typer.echo(f"account_id: {tokens.account_id or '-'}")
    typer.echo(f"auth_file: {auth_path}")
    typer.echo("usage: set BUB_MODEL=openai:gpt-5-codex and omit BUB_API_KEY")


def _prompt_for_codex_redirect(authorize_url: str) -> str:
    typer.echo("Open this URL in your browser and complete the Codex sign-in flow:\n")
    typer.echo(authorize_url)
    typer.echo("\nPaste the full callback URL or the authorization code.")
    return str(typer.prompt("callback")).strip()


def _resolve_codex_home(codex_home: Path | None) -> Path:
    if codex_home is not None:
        return codex_home.expanduser()
    return Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()


@app.command()
def openai(
    codex_home: Path | None = typer.Option(None, "--codex-home", help="Directory to store Codex OAuth credentials"),  # noqa: B008
    open_browser: bool = typer.Option(True, "--browser/--no-browser", help="Open the OAuth URL in a browser"),
    manual: bool = typer.Option(
        False,
        "--manual",
        help="Paste the callback URL or code instead of waiting for a local callback server",
    ),
    timeout_seconds: float = typer.Option(300.0, "--timeout", help="OAuth wait timeout in seconds"),
) -> None:
    """Login with OpenAI OAuth."""

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


@app.command()
def wechat(
    state_file: Path | None = typer.Option(None, "--state-file", help="Override the WeChat state file"),  # noqa: B008
) -> None:
    """Login with the official WeChat QR flow."""

    from bub.channels.wechat import login_command

    login_command(state_file=state_file)
