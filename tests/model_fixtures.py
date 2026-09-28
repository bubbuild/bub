"""Small scripted Republic providers for caller lifecycle tests; HTTP fixtures are separate."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from typing import Any

from republic import Message, Request, Response, TextPart, ToolCallPart, Usage, events


def reply(text: str = "done", calls: list[ToolCallPart] | None = None) -> Response:
    return Response(
        message=Message(role="assistant", parts=[TextPart(text=text), *(calls or [])]),
        response_id="response-1",
        response_model="actual-model",
        finish_reason="tool_call" if calls else "stop",
        usage=Usage(input_tokens=10, output_tokens=4),
    )


async def response_events(response: Response) -> AsyncGenerator[events.Event, None]:
    for index, part in enumerate(response.message.parts):
        if isinstance(part, TextPart):
            yield events.TextStart(block_id=str(index))
            yield events.TextDelta(block_id=str(index), chunk=part.text)
            yield events.TextEnd(block_id=str(index))
        elif isinstance(part, ToolCallPart):
            yield events.ToolStart(tool_call_id=part.tool_call_id, tool_name=part.tool_name)
            yield events.ToolDelta(tool_call_id=part.tool_call_id, chunk=part.tool_args)
            yield events.ToolEnd(tool_call_id=part.tool_call_id)
        else:
            raise TypeError("Use wire fixtures for native reasoning")
    yield events.StreamEnd(
        response_id=response.response_id,
        response_model=response.response_model,
        usage=response.usage,
        finish_reason=response.finish_reason,
    )


class ScriptedProvider:
    def __init__(self, respond: Callable[[Request], Awaitable[Response | AsyncIterator[events.Event]]]) -> None:
        self.respond = respond
        self.closed = False

    async def __aenter__(self) -> ScriptedProvider:
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True

    async def generate(self, request: Request) -> Response:
        raise AssertionError("Bub consumes the native stream")

    async def stream(self, request: Request) -> AsyncGenerator[events.Event, None]:
        result = await self.respond(request)
        source = response_events(result) if isinstance(result, Response) else result
        async with aclosing(source):
            async for item in source:
                yield item


def install_provider(monkeypatch: Any, runner: Any, respond: Callable) -> None:
    async def create(candidate: Any) -> ScriptedProvider:
        return ScriptedProvider(respond)

    monkeypatch.setattr(runner, "create_provider", create)
