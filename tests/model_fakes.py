"""HTTP provider fixtures shared by runtime behavior tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx2

from bub.builtin.settings import AgentSettings
from bub.core import Agent as CoreAgent
from bub.core.tape import Tape
from bub.core.tools import Tool
from bub.hooks.interception import PluginAgentHooks
from bub.prompt import UserContent
from bub.streaming import StreamEvent


class StepRunner:
    """Run one model step through the core agent, assembled from settings like the builtin agent."""

    def __init__(self, settings: AgentSettings, hooks: PluginAgentHooks | None = None) -> None:
        self.settings = settings
        self.hooks = hooks

    def run(
        self,
        *,
        tape: Tape,
        model: str,
        tools: Sequence[Tool],
        system_prompt: str | None,
        prompt: list[UserContent] | None,
        steering_messages: list[list[UserContent]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        class Agent(CoreAgent):
            async def pending_messages(self, tape: Tape) -> list[list[UserContent]]:
                return steering_messages or []

        chat_model = self.settings.chat_model(model, reasoning_effort=tape.context.state.get("reasoning_effort"))
        agent = Agent(store=tape.store, tools=tools, system_prompt=system_prompt or ())
        if self.hooks is not None:
            self.hooks.subscribe(agent.events)
        return agent.run_once(prompt, model=chat_model, tape=tape)


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

    def reply_chat(
        self,
        text: str = "done",
        calls: list[dict[str, Any]] | None = None,
        *,
        model: str = "actual-model",
        input_tokens: int = 3,
        output_tokens: int = 2,
    ) -> None:
        delta: dict[str, Any] = {"content": text}
        if calls:
            delta["tool_calls"] = [
                {"index": index, "id": call["id"], "function": {"name": call["name"], "arguments": call["arguments"]}}
                for index, call in enumerate(calls)
            ]
        self.reply(
            sse([
                {
                    "id": "response-1",
                    "model": model,
                    "choices": [{"delta": delta, "finish_reason": "tool_calls" if calls else "stop"}],
                },
                {"choices": [], "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}},
                "[DONE]",
            ])
        )

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._respond))

    def _respond(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses.pop(0)
