"""Record agent events on a tape."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import republic

from bub.core.events import ChatCompleted, CommandFinished, EventBus, RunStarted, StepFinished, StepStarted
from bub.prompt import UserContent


class TapeRecorder:
    """Write the conversation and loop progress of agent events to the event's tape.

    The agent loop reads its context back from the tape, so an agent always records
    its own events.
    """

    def subscribe(self, events: EventBus) -> None:
        events.on(RunStarted, self.on_run_started)
        events.on(StepStarted, self.on_step_started)
        events.on(StepFinished, self.on_step_finished)
        events.on(ChatCompleted, self.on_chat_completed)
        events.on(CommandFinished, self.on_command_finished)

    async def on_run_started(self, event: RunStarted) -> None:
        await event.tape.append_event(
            "loop.start", {"model": event.model, "prompt": prompt_payload(event.prompt), **event.metadata}
        )

    async def on_step_started(self, event: StepStarted) -> None:
        prompt = None if event.prompt is None else prompt_payload(event.prompt)
        await event.tape.append_event("loop.step.start", {"step": event.step, "prompt": prompt})

    async def on_step_finished(self, event: StepFinished) -> None:
        payload: dict[str, Any] = {"step": event.step, "elapsed_ms": event.elapsed_ms, "status": event.status}
        if event.error is not None:
            payload["error"] = event.error
        payload["date"] = datetime.now(UTC).isoformat()
        await event.tape.append_event("loop.step", payload)

    async def on_command_finished(self, event: CommandFinished) -> None:
        await event.tape.append_event(
            "command",
            {
                "raw": event.line,
                "name": event.name,
                "status": event.status,
                "elapsed_ms": event.elapsed_ms,
                "output": event.output,
                "date": datetime.now(UTC).isoformat(),
            },
        )

    async def on_chat_completed(self, event: ChatCompleted) -> None:
        await event.tape.record_chat(
            run_id=event.run_id,
            system_prompt=event.system_prompt,
            new_messages=event.new_messages,
            response_text=event.response_text,
            context_error=event.context_error,
            tool_calls=event.tool_calls,
            tool_results=event.tool_results,
            error=event.error,
            assistant_fields=event.assistant_fields,
            provider=event.provider,
            model=event.model,
            usage=event.usage,
        )


def prompt_payload(content: list[UserContent]) -> object:
    """Serialize prompt content for tape events."""
    return republic.user(*content).to_dict().get("content")
