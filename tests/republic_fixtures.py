"""Official SDK transports and synthetic native wire data, no credentials/live I/O."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx

from bub.builtin.context import default_tape_context
from bub.builtin.settings import AgentSettings
from bub.store import AsyncTapeStoreAdapter, FileTapeStore
from bub.tape import Tape


def settings(protocol: str = "responses", **extra: Any) -> AgentSettings:
    provider = "anthropic" if protocol == "messages" else "openai"
    values = {
        "model": f"{provider}:fixture-model",
        "model_backend": "republic",
        "api_key": "fixture-key",
        "api_base": "https://fixture.test/v1" if provider == "openai" else "https://fixture.test",
        "republic_protocols": {provider: protocol},
        **extra,
    }
    return AgentSettings.model_construct(**values)


def tape_at(path: Any) -> Tape:
    return Tape(path, AsyncTapeStoreAdapter(FileTapeStore(path)), default_tape_context()).scoped("integration")


class Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, wait: bool = False) -> None:
        self.data = data
        self.wait = wait
        self.waiting = asyncio.Event()
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data
        if self.wait:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed += 1


def sse(data: list[dict[str, Any]]) -> bytes:
    return b"".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n".encode() for item in data)


def responses(*, tool: bool = False, status: str = "completed", arguments: str = '{"value":2}') -> bytes:
    thought = {
        "type": "reasoning",
        "id": "reasoning-item",
        "summary": [],
        "encrypted_content": "opaque-reasoning",
        "status": "completed",
    }
    call = {
        "type": "function_call",
        "id": "function-item",
        "call_id": "call-original",
        "name": "inspect",
        "arguments": arguments,
        "status": "completed",
    }
    text = {
        "type": "message",
        "id": "message-item",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "finished", "annotations": []}],
    }
    output = [thought, call] if tool else [text]
    chunks: list[dict[str, Any]] = []
    if tool:
        chunks = [
            {"type": "response.output_item.added", "output_index": 0, "item": {**thought, "status": "in_progress"}},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**call, "arguments": "", "status": "in_progress"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "item_id": "function-item",
                "delta": arguments,
            },
            {"type": "response.output_item.done", "output_index": 1, "item": call},
        ]
    result = {
        "object": "response",
        "id": "response-original",
        "model": "resolved-model",
        "created_at": 1,
        "status": status,
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    if status == "failed":
        result["error"] = {"code": "fixture", "message": "failed"}
    chunks.append({"type": f"response.{status}", "response": result})
    return sse([{**item, "sequence_number": i} for i, item in enumerate(chunks)])


def messages(*, tool: bool = False, stop: str | None = None) -> bytes:
    data = [
        {
            "type": "message_start",
            "message": {
                "id": "message-original",
                "type": "message",
                "role": "assistant",
                "model": "resolved-model",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                },
            },
        }
    ]
    if tool:
        data += [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque"}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "call-original", "name": "inspect", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"value":2}'},
            },
            {"type": "content_block_stop", "index": 1},
        ]
    else:
        data += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "finished"}},
            {"type": "content_block_stop", "index": 0},
        ]
    data += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop or ("tool_use" if tool else "end_turn"), "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    return sse(data)


def chat(*, tool: bool = False) -> bytes:
    change = (
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-original",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": '{"value":2}'},
                }
            ]
        }
        if tool
        else {"content": "finished"}
    )
    chunks = [
        {"choices": [{"index": 0, "delta": change, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    return (
        b"".join(
            (
                "data: "
                + json.dumps({
                    "id": "chat-original",
                    "object": "chat.completion.chunk",
                    "model": "resolved-model",
                    "created": 1,
                    **item,
                })
                + "\n\n"
            ).encode()
            for item in chunks
        )
        + b"data: [DONE]\n\n"
    )


def wire(protocol: str, **kwargs: Any) -> bytes:
    return {"responses": responses, "messages": messages, "chat": chat}[protocol](**kwargs)


class Transport(httpx.MockTransport):
    def __init__(self, replies: list[Body | httpx.Response]) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []
        self.closed = 0
        super().__init__(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.replies.pop(0)
        return (
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=response)
            if isinstance(response, Body)
            else response
        )

    async def aclose(self) -> None:
        self.closed += 1

    def payload(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


@contextmanager
def sdk_transport(transport: Transport) -> Iterator[list[httpx.AsyncClient]]:
    clients = []

    def create(**kwargs: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(**kwargs, transport=transport)
        clients.append(client)
        return client

    with (
        patch("openai._base_client.AsyncHttpxClientWrapper", create),
        patch("anthropic._base_client.AsyncHttpxClientWrapper", create),
    ):
        yield clients
