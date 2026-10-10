from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest

from bub.builtin.context import default_tape_context
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings
from bub.channels.message import ChannelMessage, MediaItem
from bub.errors import BubError, ErrorKind
from bub.framework import BubFramework
from bub.store import AsyncTapeStoreAdapter, FileTapeStore, InMemoryTapeStore
from bub.tape import Tape, TapeContext
from bub.tools import Tool
from tests.model_fakes import ProviderService, chat_events, sse


@pytest.mark.asyncio
async def test_channel_media_reaches_gemini_and_survives_tape_reload(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    async def image():
        return b"image"

    async def audio():
        return b"audio"

    async def video():
        return b"video"

    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    channel = ChannelMessage(
        session_id="media",
        channel="telegram",
        content="Describe the attachments.",
        media=[
            MediaItem("image", "image/png", data_fetcher=image),
            MediaItem("audio", "audio/ogg", data_fetcher=audio),
            MediaItem("video", "video/mp4", data_fetcher=video),
        ],
    )
    prompt = await framework.build_prompt(channel, channel.session_id, {})
    for _ in range(2):
        provider_service.reply(
            sse([{"candidates": [{"content": {"parts": [{"text": "ready"}]}, "finishReason": "STOP"}]}])
        )
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped("media")
        await tape.ensure_bootstrap_anchor()
        events = [
            event
            async for event in runner.run(tape=tape, model="google:test", tools=[], system_prompt=None, prompt=prompt)
        ]
        reopened = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped(
            "media"
        )
        follow_up = [
            event
            async for event in runner.run(
                tape=reopened, model="google:test", tools=[], system_prompt=None, prompt="Continue."
            )
        ]
    expected = [
        {"inlineData": {"mimeType": "image/png", "data": "aW1hZ2U="}},
        {"inlineData": {"mimeType": "audio/ogg", "data": "YXVkaW8="}},
        {"inlineData": {"mimeType": "video/mp4", "data": "dmlkZW8="}},
    ]
    assert provider_service.body(0)["contents"][0]["parts"][1:] == expected
    assert provider_service.body(1)["contents"][0]["parts"][1:] == expected
    assert events[-1].data == {"ok": True, "text": "ready"}
    assert follow_up[-1].data == {"ok": True, "text": "ready"}


@pytest.mark.asyncio
@pytest.mark.parametrize("external_client", [False, True])
async def test_consumer_close_releases_the_request_and_respects_client_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_client: bool
) -> None:
    closed = asyncio.Event()

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, stream=Body()))
    ) as client:
        monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: client)
        client_args = {"api_format": "chat"}
        if external_client:
            client_args["http_client"] = client
        runner = ModelRunner(AgentSettings(client_args=client_args))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("cancel")
        events = runner.run(tape=tape, model="openai:test", tools=[], system_prompt=None, prompt="hello")
        assert (await anext(events)).data == {"delta": "first"}
        await events.aclose()
        assert closed.is_set()
        assert client.is_closed is not external_client
        assert not await tape.store.fetch_all(tape.query().kinds("message"))


@pytest.mark.asyncio
async def test_primary_success_does_not_require_an_available_fallback(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        runner = ModelRunner(
            AgentSettings(
                fallback_models=["not-installed:test"], client_args={"http_client": client, "api_format": "chat"}
            )
        )
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("fallback")
        events = [
            event
            async for event in runner.run(tape=tape, model="openai:test", tools=[], system_prompt=None, prompt="hello")
        ]
    assert len(provider_service.requests) == 1
    assert events[-1].data == {"ok": True, "text": "done"}


@pytest.mark.asyncio
async def test_fresh_process_replays_tool_signatures_to_the_provider(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(
        sse([
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {"id": "call-1", "name": "lookup", "args": {}},
                                    "thoughtSignature": "signature",
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
        ])
    )
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped(
            "persisted"
        )
        await tape.ensure_bootstrap_anchor()
        _ = [
            event
            async for event in runner.run(
                tape=tape,
                model="google:test",
                tools=[Tool(name="lookup", handler=lambda: "lookup result")],
                system_prompt=None,
                prompt="Look it up.",
            )
        ]
    code = """
import asyncio, json, sys
from pathlib import Path
import httpx2
from bub.builtin.context import default_tape_context
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings
from bub.store import FileTapeStore, AsyncTapeStoreAdapter
from bub.tape import Tape
async def main():
    bodies = []
    def respond(request):
        bodies.append(json.loads(request.content))
        event = {"candidates": [{"content": {"parts": [{"text": "ready"}]}, "finishReason": "STOP"}]}
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(event) + "\\n\\n")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        directory = Path(sys.argv[1])
        tape = Tape(directory, AsyncTapeStoreAdapter(FileTapeStore(directory)), default_tape_context()).scoped("persisted")
        runner = ModelRunner(AgentSettings(client_args={"http_client": client}))
        events = [event async for event in runner.run(tape=tape, model="google:test", tools=[], system_prompt=None, prompt="Continue.")]
    print(json.dumps({"request": bodies[0], "result": events[-1].data}))
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=True)
    replay = json.loads(result.stdout)
    assistant = next(content for content in replay["request"]["contents"] if content["role"] == "model")
    call = next(part for part in assistant["parts"] if "functionCall" in part)
    assert call["functionCall"] == {"id": "call-1", "name": "lookup", "args": {}}
    assert call["thoughtSignature"] == "signature"
    assert "lookup result" in json.dumps(replay["request"])
    assert replay["result"] == {"ok": True, "text": "ready"}


@pytest.mark.asyncio
async def test_media_urls_preserve_explicit_mime_types(tmp_path: Path, provider_service: ProviderService) -> None:
    provider_service.reply(sse([{"candidates": [{"content": {"parts": [{"text": "ready"}]}, "finishReason": "STOP"}]}]))
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("urls")
        events = [
            event
            async for event in runner.run(
                tape=tape,
                model="google:test",
                tools=[],
                system_prompt=None,
                prompt=[
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "url": "https://example.test/photo.png?signature=opaque",
                    },
                    {"type": "image", "media_type": "image/webp", "url": "https://example.test/attachment"},
                ],
            )
        ]
    assert provider_service.body()["contents"][0]["parts"] == [
        {"fileData": {"mimeType": "image/png", "fileUri": "https://example.test/photo.png?signature=opaque"}},
        {"fileData": {"mimeType": "image/webp", "fileUri": "https://example.test/attachment"}},
    ]
    assert events[-1].data == {"ok": True, "text": "ready"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "url": "https://example.test/photo"},
        {"type": "audio", "media_type": "audio/wav"},
        {"type": "document", "media_type": "application/pdf", "url": "https://example.test/file.pdf"},
    ],
)
async def test_invalid_content_fails_without_a_provider_request(
    tmp_path: Path, provider_service: ProviderService, block: dict
) -> None:
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("invalid")
        with pytest.raises(BubError) as exc:
            _ = [
                event
                async for event in runner.run(
                    tape=tape, model="google:test", tools=[], system_prompt=None, prompt=[block]
                )
            ]
    assert exc.value.kind == ErrorKind.INVALID_INPUT
    assert not provider_service.requests
