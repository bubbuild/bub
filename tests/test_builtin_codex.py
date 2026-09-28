"""Bub login UX -> Republic Authlib -> native Codex SSE, using synthetic credentials."""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from republic import IncompleteStreamError, ProviderError, UnsupportedRequestError
from republic.auth import codex
from republic.auth.codex import CodexAuthError, CodexTokens
from republic_fixtures import Body, Transport, responses, sdk_transport, tape_at
from test_republic_integration import collect

from bub.builtin import auth
from bub.builtin.model_provider import protocol_for
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings
from bub.tools import Tool


def jwt(account="acct_test", expiry=None):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account}}
    if expiry is not None:
        payload["exp"] = expiry
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{encoded}.fixture"


def save(home, *, expired=False):
    tokens = CodexTokens(jwt(), "fixture-refresh", time.time() + (-1 if expired else 3600), "acct_test")
    path = auth.codex_token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    auth.save_codex_tokens(path, tokens)
    return tokens


def config(home, **extra):
    return AgentSettings.model_construct(model="openai:codex-fixture", codex_home=home, **extra)


def selected(settings):
    return protocol_for(settings, settings.model_candidates(settings.model)[0])


@pytest.mark.parametrize("expiry", [1900000000, "2030-03-17T17:46:40+00:00", None])
def test_existing_auth_file_is_read_without_migration_or_write(tmp_path, expiry):
    tokens = {"access_token": jwt(expiry=1900000000), "refresh_token": "fixture-refresh"}
    if expiry is not None:
        tokens["expires_at"] = expiry
    old = json.dumps({"tokens": tokens, "last_refresh": "2000-01-01T00:00:00Z"})
    path = tmp_path / "auth.json"
    path.write_text(old)
    assert selected(config(tmp_path)) == "openai.codex"
    loaded = auth.load_codex_tokens(tmp_path)
    assert loaded.account_id == "acct_test" and loaded.expires_at == 1900000000
    assert path.read_text() == old and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {},
        {"tokens": {}},
        {"tokens": {"access_token": "secret"}},
        {"tokens": {"access_token": [], "refresh_token": "secret"}},
        {"tokens": {"access_token": "", "refresh_token": "secret"}},
    ],
)
def test_bad_file_does_not_select_codex_or_expose_secrets(tmp_path, raw):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(raw))
    assert selected(config(tmp_path)) == "openai.chat"
    assert auth.load_codex_tokens(tmp_path) is None


def test_selection_preserves_api_key_base_and_explicit_protocol_precedence(tmp_path):
    assert selected(config(tmp_path)) == "openai.chat"
    (tmp_path / "auth.json").write_text("invalid json")
    assert selected(config(tmp_path)) == "openai.chat"
    (tmp_path / "auth.json").unlink()
    save(tmp_path)
    assert selected(config(tmp_path)) == "openai.codex"
    assert selected(config(tmp_path, api_key="fixture-key")) == "openai.chat"
    assert selected(config(tmp_path, api_key=jwt())) == "openai.codex"
    assert selected(config(tmp_path, api_base="https://fixture.test")) == "openai.chat"
    assert selected(config(tmp_path, api_key=jwt(), api_base="https://fixture.test")) == "openai.chat"
    assert selected(config(tmp_path, republic_protocols={"openai": "responses"})) == "openai.responses"
    settings = AgentSettings.model_construct(model="openrouter:codex-fixture", codex_home=tmp_path)
    assert selected(settings) == "openrouter.chat"


@pytest.mark.asyncio
async def test_login_inference_refresh_and_second_native_inference(tmp_path, monkeypatch):
    auth_requests = []

    def token_endpoint(request):
        auth_requests.append(request)
        values = {
            "access_token": jwt("acct_new" if len(auth_requests) == 2 else "acct_test"),
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        if len(auth_requests) == 1:
            values["refresh_token"] = "fixture-refresh"  # noqa: S105 - synthetic fixture
        return httpx.Response(200, json=values)

    transport = httpx.MockTransport(token_endpoint)
    exchange, refresh = codex.exchange_code, codex.refresh_tokens

    async def exchange_offline(authorization, callback):
        return await exchange(authorization, callback, transport=transport)

    async def refresh_offline(tokens):
        return await refresh(tokens, transport=transport)

    monkeypatch.setattr(codex, "exchange_code", exchange_offline)
    monkeypatch.setattr(codex, "refresh_tokens", refresh_offline)

    def callback(url):
        query = parse_qs(urlsplit(url).query)
        assert query["code_challenge_method"] == ["S256"]
        return query["redirect_uri"][0] + "?state=" + query["state"][0] + "&code=fixture-code"

    tokens = await auth.login_openai_codex_oauth(codex_home=tmp_path, open_browser=False, prompt_for_redirect=callback)
    assert auth.load_codex_tokens(tmp_path) == tokens
    http = Transport([Body(responses(tool=True)), Body(responses())])
    tool_calls = []

    def inspect(value: int):
        tool_calls.append(value)
        return "checked"

    settings = config(tmp_path)
    tape = tape_at(tmp_path / "tape")
    with sdk_transport(http) as clients:
        await collect(ModelRunner(settings), tape, tools=[Tool.from_callable(inspect)])
        auth.save_codex_tokens(
            auth.codex_token_path(tmp_path),
            CodexTokens(tokens.access_token, tokens.refresh_token, time.time() - 1, tokens.account_id),
        )
        output = await collect(ModelRunner(settings), tape_at(tmp_path / "tape"), prompt=None)
    assert len(http.requests) == 2 and len(auth_requests) == 2 and tool_calls == [2]
    assert all(client.is_closed for client in clients)
    assert output[-1].data["text"] == "finished"
    assert all(str(request.url) == "https://chatgpt.com/backend-api/codex/responses" for request in http.requests)
    assert [request.headers["chatgpt-account-id"] for request in http.requests] == ["acct_test", "acct_new"]
    first, second = http.payload(0), http.payload(1)
    assert first["store"] is False and first["stream"] is True and first["instructions"] == ""
    assert first["include"] == ["reasoning.encrypted_content"] and "max_output_tokens" not in first
    assert (
        next(item for item in second["input"] if item.get("type") == "reasoning")["encrypted_content"]
        == "opaque-reasoning"
    )
    call = next(item for item in second["input"] if item.get("type") == "function_call")
    assert call["id"] == "function-item" and call["call_id"] == "call-original"
    assert (
        next(item for item in second["input"] if item.get("type") == "function_call_output")["call_id"]
        == "call-original"
    )
    saved = auth.load_codex_tokens(tmp_path)
    assert saved.account_id == "acct_new" and saved.refresh_token == tokens.refresh_token
    grants = [parse_qs(request.content.decode()) for request in auth_requests]
    assert grants[0]["grant_type"] == ["authorization_code"] and "code_verifier" in grants[0]
    assert grants[1]["grant_type"] == ["refresh_token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_query",
    [
        "code=fixture-code",
        "code=fixture-code&state=wrong",
        "error=access_denied&state={state}",
        "",
        "code=&state={state}",
    ],
)
async def test_invalid_callback_never_writes_credentials(tmp_path, callback_query):
    def callback(url):
        query = parse_qs(urlsplit(url).query)
        return query["redirect_uri"][0] + "?" + callback_query.format(state=query["state"][0])

    with pytest.raises(CodexAuthError):
        await auth.login_openai_codex_oauth(codex_home=tmp_path, open_browser=False, prompt_for_redirect=callback)
    assert not auth.codex_token_path(tmp_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_callback_timeout_and_cancel_release_receiver(tmp_path, monkeypatch, cancel):
    exited = []
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    @contextmanager
    def receiver(uri):
        def wait(timeout):
            loop.call_soon_threadsafe(entered.set)
            raise queue.Empty

        try:
            yield wait
        finally:
            exited.append(True)

    monkeypatch.setattr(auth, "_callback_receiver", receiver)
    if cancel:

        async def exchange(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(codex, "exchange_authorization_code", exchange)
        task = asyncio.create_task(
            auth.login_openai_codex_oauth(
                codex_home=tmp_path, open_browser=False, prompt_for_redirect=lambda _: "callback"
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CodexAuthError, match="callback_timeout"):
            await auth.login_openai_codex_oauth(codex_home=tmp_path, open_browser=False, timeout_seconds=0.01)
        assert exited == [True]
    assert not auth.codex_token_path(tmp_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_inference_error_does_not_refresh_or_retry(tmp_path, monkeypatch, status):
    save(tmp_path)

    async def unexpected(*args):
        pytest.fail("401 must not trigger refresh/replay")

    monkeypatch.setattr(codex, "refresh_tokens", unexpected)
    transport = Transport([httpx.Response(status, json={"error": {"message": "secret-access"}})])
    with sdk_transport(transport) as clients, pytest.raises(ProviderError) as error:
        await collect(ModelRunner(config(tmp_path)), tape_at(tmp_path / "tape"))
    assert error.value.status_code == status
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert "secret-access" not in repr(error.value)
    assert len(transport.requests) == 1 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_failed_pre_call_refresh_does_not_use_stale_token(tmp_path, monkeypatch):
    save(tmp_path, expired=True)
    refresh = codex.refresh_tokens
    token_http = Transport([httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret"})])

    async def refresh_offline(tokens):
        return await refresh(tokens, transport=token_http)

    monkeypatch.setattr(codex, "refresh_tokens", refresh_offline)
    inference = Transport([])
    with sdk_transport(inference), pytest.raises(CodexAuthError):
        await collect(ModelRunner(config(tmp_path)), tape_at(tmp_path / "tape"))
    assert inference.requests == [] and len(token_http.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"max_tokens": 100},
        {"completion_args": {"temperature": 0.2}},
    ],
)
async def test_codex_unsupported_options_are_not_silently_removed(tmp_path, extra):
    save(tmp_path)
    transport = Transport([])
    with sdk_transport(transport), pytest.raises(UnsupportedRequestError):
        await collect(ModelRunner(config(tmp_path, **extra)), tape_at(tmp_path / "tape"))
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_local_callback_server_closes_after_wait(tmp_path, cancel):
    import socket

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    opened = asyncio.Event()
    task = asyncio.create_task(
        auth.login_openai_codex_oauth(
            codex_home=tmp_path,
            redirect_uri=f"http://127.0.0.1:{port}/auth/callback",
            timeout_seconds=0.03,
            browser_opener=lambda _: opened.set(),
        )
    )
    await opened.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CodexAuthError, match="callback_timeout"):
            await task
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    assert not auth.codex_token_path(tmp_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["close", "cancel", "missing"])
async def test_codex_partial_stream_never_executes_tools_and_releases_resources(tmp_path, mode):
    from contextlib import aclosing

    from test_republic_integration import partial

    save(tmp_path)
    body = Body(partial("responses"), wait=mode != "missing")
    http = Transport([body])
    tape = tape_at(tmp_path / "tape")
    await tape.ensure_bootstrap_anchor()
    with sdk_transport(http) as clients:
        output = ModelRunner(config(tmp_path)).run(
            tape=tape, model="openai:codex-fixture", tools=[], system_prompt=None, prompt="hello"
        )
        async with aclosing(output):
            assert (await anext(output)).kind == "text"
            if mode == "cancel":
                task = asyncio.create_task(anext(output))
                await body.waiting.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode == "missing":
                with pytest.raises(IncompleteStreamError):
                    await anext(output)
    assert body.closed == 1 and len(http.requests) == 1
    assert all(client.is_closed for client in clients)
    assert not await tape.store.fetch_all(tape.query().kinds("tool_call"))


@pytest.mark.asyncio
async def test_explicit_codex_allows_caller_access_token_and_endpoint(tmp_path):
    transport = Transport([Body(responses())])
    with sdk_transport(transport):
        await collect(
            ModelRunner(
                config(
                    tmp_path,
                    republic_protocols={"openai": "codex"},
                    api_key="direct-access",
                    api_base="https://custom.test/v1",
                )
            ),
            tape_at(tmp_path / "tape"),
        )
    assert transport.requests[0].url.host == "custom.test"
    assert transport.requests[0].headers["authorization"] == "Bearer direct-access"
    assert not auth.codex_token_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_codex_native_options_and_reasoning_effort_reach_the_wire(tmp_path):
    save(tmp_path)
    settings = config(tmp_path, completion_args={"provider_options": {"text": {"verbosity": "low"}}})
    tape = tape_at(tmp_path / "tape")
    tape.context.state["reasoning_effort"] = "high"
    transport = Transport([Body(responses())])
    with sdk_transport(transport):
        await collect(ModelRunner(settings), tape)
    assert transport.payload()["text"] == {"verbosity": "low"}
    assert transport.payload()["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_valid_existing_credentials_infer_without_refresh_or_file_changes(tmp_path, monkeypatch):
    save(tmp_path)
    original = auth.codex_token_path(tmp_path).read_bytes()

    async def unexpected(*args):
        pytest.fail("Valid credentials must not be refreshed")

    monkeypatch.setattr(codex, "refresh_tokens", unexpected)
    http = Transport([Body(responses())])
    with sdk_transport(http):
        await collect(ModelRunner(config(tmp_path)), tape_at(tmp_path / "tape"))
    assert len(http.requests) == 1 and auth.codex_token_path(tmp_path).read_bytes() == original
    assert http.requests[0].headers["originator"] == "bub"


@pytest.mark.asyncio
@pytest.mark.parametrize("write_failure", [False, True])
async def test_refresh_rotates_original_file_retaining_unknown_fields_before_inference(
    tmp_path, monkeypatch, write_failure
):
    save(tmp_path, expired=True)
    path = auth.codex_token_path(tmp_path)
    raw = json.loads(path.read_text())
    raw.update(untouched={"custom": 1})
    raw["tokens"].update({"id_token": "opaque-id", "other_token_field": {"value": 2}})
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    refresh = codex.refresh_tokens
    auth_http = Transport([
        httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )
    ])

    async def offline(tokens):
        return await refresh(tokens, transport=auth_http)

    monkeypatch.setattr(codex, "refresh_tokens", offline)
    if write_failure:

        def fail_replace(*args):
            raise OSError("secret-from-filesystem")

        monkeypatch.setattr(auth.os, "replace", fail_replace)
    inference = Transport([] if write_failure else [Body(responses())])
    with sdk_transport(inference):
        if write_failure:
            with pytest.raises(CodexAuthError) as error:
                await collect(ModelRunner(config(tmp_path)), tape_at(tmp_path / "tape"))
            assert error.value.code == "credential_write_failed"
            assert error.value.__cause__ is None and error.value.__context__ is None
            assert "secret" not in repr(error.value) and path.read_bytes() == original
            assert not inference.requests
        else:
            await collect(ModelRunner(config(tmp_path)), tape_at(tmp_path / "tape"))
            saved = json.loads(path.read_text())
            assert saved["untouched"] == raw["untouched"]
            assert saved["tokens"]["other_token_field"] == {"value": 2}
            assert saved["tokens"]["id_token"] == raw["tokens"]["id_token"]
            assert saved["tokens"]["account_id"] == "acct_test"
            assert saved["tokens"]["refresh_token"] == "rotated-refresh"  # noqa: S105 - synthetic fixture
            assert isinstance(saved["tokens"]["expires_at"], str)
            assert saved["last_refresh"] != raw["last_refresh"]
            assert path.stat().st_mode & 0o777 == 0o600
            assert len(inference.requests) == 1
            assert inference.requests[0].headers["authorization"] == "Bearer rotated-access"
    assert len(auth_http.requests) == 1


@pytest.mark.asyncio
async def test_early_refresh_failure_uses_still_valid_old_token_without_rewriting(tmp_path, monkeypatch):
    tokens = save(tmp_path)
    auth.save_codex_tokens(
        auth.codex_token_path(tmp_path),
        CodexTokens(tokens.access_token, tokens.refresh_token, time.time() + 60, tokens.account_id),
    )
    original = auth.codex_token_path(tmp_path).read_bytes()
    refresh = codex.refresh_tokens
    auth_http = Transport([httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret"})])

    async def offline(tokens):
        return await refresh(tokens, transport=auth_http)

    monkeypatch.setattr(codex, "refresh_tokens", offline)
    inference = Transport([Body(responses())])
    with sdk_transport(inference):
        await collect(ModelRunner(config(tmp_path)), tape_at(tmp_path / "tape"))
    assert len(auth_http.requests) == len(inference.requests) == 1
    assert inference.requests[0].headers["authorization"] == f"Bearer {tokens.access_token}"
    assert auth.codex_token_path(tmp_path).read_bytes() == original


@pytest.mark.parametrize(
    "fields,access_exp,expected",
    [
        ({"expires_at": 1900000200}, 1900000100, 1900000200),
        ({}, 1900000100, 1900000100),
        ({"last_refresh": 1900000000}, None, 1900003600),
        ({"last_refresh": "2030-03-17T17:46:40Z"}, None, 1900003600),
        ({}, None, 1900003600),
    ],
)
def test_original_expiry_priority_and_fallback_are_bub_policy(tmp_path, monkeypatch, fields, access_exp, expected):
    monkeypatch.setattr(auth.time, "time", lambda: 1900000000)
    raw = {"tokens": {"access_token": jwt(expiry=access_exp), "refresh_token": "refresh"}}
    if "expires_at" in fields:
        raw["tokens"]["expires_at"] = fields["expires_at"]
    if "last_refresh" in fields:
        raw["last_refresh"] = fields["last_refresh"]
    path = auth.codex_token_path(tmp_path)
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    assert auth.load_codex_tokens(tmp_path).expires_at == expected
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_manual_bare_code_uses_republic_pkce_and_writes_original_auth_format(tmp_path, monkeypatch):
    transport = Transport([
        httpx.Response(
            200,
            json={
                "access_token": jwt(),
                "refresh_token": "refresh",
                "expires_in": 3600,
            },
        )
    ])
    exchange = codex.exchange_authorization_code

    async def offline(authorization, code):
        return await exchange(authorization, code, transport=transport)

    monkeypatch.setattr(codex, "exchange_authorization_code", offline)
    (tmp_path / "auth.json").write_text("old malformed layout")
    await auth.login_openai_codex_oauth(
        codex_home=tmp_path, open_browser=False, prompt_for_redirect=lambda _: "manual-code"
    )
    body = parse_qs(transport.requests[0].content.decode())
    assert body["code"] == ["manual-code"] and "code_verifier" in body
    saved = json.loads((tmp_path / "auth.json").read_text())
    assert saved["tokens"]["refresh_token"] == "refresh" and saved["last_refresh"]  # noqa: S105 - fixture
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_recognizable_access_token_infers_without_file_refresh_or_expiry(tmp_path):
    transport = Transport([Body(responses())])
    with sdk_transport(transport):
        await collect(ModelRunner(config(tmp_path, api_key=jwt())), tape_at(tmp_path / "tape"))
    assert str(transport.requests[0].url) == "https://chatgpt.com/backend-api/codex/responses"
    assert transport.requests[0].headers["chatgpt-account-id"] == "acct_test"
    assert len(transport.requests) == 1 and not auth.codex_token_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_malformed_manual_url_is_a_sanitized_login_error(tmp_path):
    with pytest.raises(CodexAuthError, match="invalid_callback") as caught:
        await auth.login_openai_codex_oauth(
            codex_home=tmp_path,
            open_browser=False,
            prompt_for_redirect=lambda _: "https://[private-code",
        )
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "private" not in repr(caught.value) and not auth.codex_token_path(tmp_path).exists()
