"""An HTTP provider that records requests and replays queued chat completion streams."""

from __future__ import annotations

import json
from typing import Any

import httpx2


def sse(events: list[Any]) -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join(f"data: {event if isinstance(event, str) else json.dumps(event)}\n\n" for event in events),
    )


def chat_events(text: str = "done") -> list[Any]:
    return [
        {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        "[DONE]",
    ]


def tool_events(calls: list[dict[str, Any]], text: str = "") -> list[Any]:
    return [
        {"choices": [{"delta": {"content": text, "tool_calls": calls}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    ]


class ProviderService:
    """Record HTTP requests and return queued provider responses."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.responses: list[httpx2.Response] = []

    def reply(self, response: httpx2.Response) -> None:
        self.responses.append(response)

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._respond))

    def _respond(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)
