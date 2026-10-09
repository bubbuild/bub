from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bub.builtin.auth import app
from bub.builtin.context import default_tape_context
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings
from bub.channels.message import ChannelMessage, MediaItem
from bub.framework import BubFramework
from bub.prompt import to_content
from bub.store import AsyncTapeStoreAdapter, FileTapeStore, InMemoryTapeStore
from bub.tape import Tape, TapeContext, TapeEntry
from bub.tools import Tool
from tests.model_fakes import ProviderService, sse


@pytest.mark.asyncio
@pytest.mark.parametrize("content_kind", ["string", "text", "image", "assistant-history"])
async def test_codex_sends_content_with_file_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_service: ProviderService, content_kind: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token", "account_id": "test-account"}})
    )
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    message = ChannelMessage(session_id="content", channel="cli", content="Reply BUB_READY.")
    if content_kind == "image":
        message.media = [MediaItem("image", "image/png", url="https://example.test/image.png")]
    prompt = await framework.build_prompt(message, message.session_id, {})
    if content_kind == "text":
        prompt = [{"type": "text", "text": message.content}]
    provider_service.reply(
        sse([
            {"type": "response.output_text.delta", "delta": "BUB_READY"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ])
    )
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), default_tape_context()).scoped("content")
        await tape.ensure_bootstrap_anchor()
        if content_kind == "assistant-history":
            await tape.store.append(
                tape.name,
                TapeEntry.message({
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Checking."}],
                    "tool_calls": [{"id": "call-1", "name": "check", "arguments": "{}"}],
                }),
            )
            await tape.store.append(
                tape.name, TapeEntry.message({"role": "tool", "tool_call_id": "call-1", "content": "Ready"})
            )
        events = [
            event
            async for event in runner.run(
                tape=tape, model="codex:test", tools=[], system_prompt=None, prompt=to_content(prompt)
            )
        ]
    request = provider_service.requests[0]
    body = provider_service.body()
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["ChatGPT-Account-Id"] == "test-account"
    assert body["store"] is False
    assert body["stream"] is True
    assert "max_output_tokens" not in body
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["input"][-1]["content"][0]["type"] == "input_text"
    if content_kind == "image":
        assert body["input"][-1]["content"][-1] == {
            "type": "input_image",
            "image_url": "https://example.test/image.png",
        }
    if content_kind == "assistant-history":
        assert body["input"][1]["type"] == "function_call"
        assert body["input"][2]["type"] == "function_call_output"
    assert events[-1].data == {"ok": True, "text": "BUB_READY"}


@pytest.mark.asyncio
async def test_codex_replays_encrypted_reasoning_and_tool_results_after_reload(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    reasoning = {"type": "reasoning", "id": "reason-1", "encrypted_content": "opaque", "summary": []}
    provider_service.reply(
        sse([
            {"type": "response.output_item.done", "item": reasoning},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "echo",
                    "arguments": "{}",
                },
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ])
    )
    provider_service.reply(
        sse([
            {"type": "response.output_text.delta", "delta": "done"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ])
    )
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(api_key="test", client_args={"http_client": client}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped("codex")
        await tape.ensure_bootstrap_anchor()
        tools = [Tool(name="echo", handler=lambda: "echoed")]
        _ = [
            event
            async for event in runner.run(
                tape=tape, model="codex:test", tools=tools, system_prompt=None, prompt=to_content("echo")
            )
        ]
        reopened = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped(
            "codex"
        )
        events = [
            event
            async for event in runner.run(
                tape=reopened, model="codex:test", tools=tools, system_prompt=None, prompt=None
            )
        ]
    assert provider_service.body()["input"][1:] == [
        reasoning,
        {"type": "function_call", "call_id": "call-1", "name": "echo", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-1", "output": "echoed"},
    ]
    assert events[-1].data == {"ok": True, "text": "done"}


@pytest.mark.asyncio
async def test_named_endpoint_falls_back_to_codex_with_its_own_credentials(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    import httpx2

    provider_service.reply(httpx2.Response(503, json={"error": {"message": "unavailable"}}))
    provider_service.reply(
        sse([
            {"type": "response.output_text.delta", "delta": "ready"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ])
    )
    async with provider_service.client() as client:
        runner = ModelRunner(
            AgentSettings(
                model="relay:sample",
                fallback_models=["codex:sample"],
                api_key={"codex": "codex-key"},
                providers={"relay": {"type": "openai", "api_key": "relay-key", "api_base": "https://relay.test/v1"}},
                client_args={"http_client": client, "max_retries": 0},
            )
        )
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("fallback")
        events = [
            event
            async for event in runner.run(
                tape=tape, model="relay:sample", tools=[], system_prompt=None, prompt=to_content("hello")
            )
        ]
    assert [str(request.url) for request in provider_service.requests] == [
        "https://relay.test/v1/responses",
        "https://chatgpt.com/backend-api/codex/responses",
    ]
    assert [request.headers["authorization"] for request in provider_service.requests] == [
        "Bearer relay-key",
        "Bearer codex-key",
    ]
    assert events[-1].data == {"ok": True, "text": "ready"}


@pytest.mark.parametrize("device_auth", [False, True])
@pytest.mark.parametrize("directory_option", [False, True])
def test_login_stores_credentials_in_the_selected_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codex_executable: Path, device_auth: bool, directory_option: bool
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "default-home"))
    directory = tmp_path / "selected-home" if directory_option else tmp_path / "default-home"
    args = ["codex", "--executable", str(codex_executable)]
    if directory_option:
        args.extend(["--codex-home", str(directory)])
    if device_auth:
        args.append("--device-auth")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    account = "device-account" if device_auth else "browser-account"
    assert f"account_id: {account}" in result.output
    assert json.loads((directory / "auth.json").read_text())["tokens"]["account_id"] == account
    assert "test-token" not in result.output
    assert os.environ["CODEX_HOME"] == str(tmp_path / "default-home")
    if directory_option:
        assert not (tmp_path / "default-home" / "auth.json").exists()


@pytest.mark.parametrize("directory_option", [False, True])
def test_login_reports_a_missing_executable(tmp_path: Path, directory_option: bool) -> None:
    args = ["codex", "--executable", str(tmp_path / "missing")]
    if directory_option:
        args.extend(["--codex-home", str(tmp_path)])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "Cannot start" in result.output


@pytest.mark.parametrize("directory_option", [False, True])
def test_failed_login_does_not_report_saved_credentials_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_option: bool
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token", "account_id": "saved-account"}})
    )
    executable = tmp_path / "failed-codex"
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(2)\n")
    executable.chmod(0o755)
    args = ["codex", "--executable", str(executable)]
    if directory_option:
        args.extend(["--codex-home", str(tmp_path)])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "login: failed" in result.output
    assert "saved-account" not in result.output
    assert "login: ok" not in result.output
