---
title: Python SDK
description: Embed Bub's agent loop in a Python application with your own model, tools, prompts, and session storage.
sidebar:
  order: 0
---

`bub.core` is Bub's agent kernel. Its `Agent` runs the agent loop (call the model, execute the tools it asks for,
repeat until it answers) over components you supply: tools, system prompt sections, and a tape store, plus the
Republic model passed to each run. It reads no Bub settings and loads no plugins, so it embeds in any application.

Bub's own runtime is built on the same kernel: `bub.builtin.BubAgent` adds settings, plugin hooks, skills,
commands, and steering. See [Bub's agent](#bubs-agent) at the end of this page.

## Install

Install Bub in your Python 3.12+ project:

```bash
uv add bub
```

The API below describes this source checkout; use a release containing these interfaces or install the checkout.

## Create an agent

Save the following as `sdk_example.py`:

```python
import asyncio
from pathlib import Path

import republic

from bub.core import Agent, Tool
from bub.core.store import FileTapeStore


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up the status of an order."""
    # Replace this demonstration response with your application's order service.
    return {"order_id": order_id, "status": "shipped"}


def create_agent() -> Agent:
    root = Path(__file__).resolve().parent
    return Agent(
        store=FileTapeStore(root / "sessions"),
        tools=[Tool.from_callable(lookup_order)],
        system_prompt=[
            "You are an order assistant. Reply directly to the user.",
            "Use lookup_order to check order status before answering.",
        ],
    )


async def main() -> None:
    agent = create_agent()
    # Any Republic "provider:model" spec. The key comes from REPUBLIC_OPENAI_API_KEY.
    async with republic.get_model("openai:gpt-5-mini") as model:
        stream = await agent.run(
            ["Where is order A123?"], model=model, options={"max_tokens": 4096}, tape="customer-42"
        )
        async for event in stream:
            if event.kind == "text":
                print(event.data.get("delta", ""), end="", flush=True)
            elif event.kind == "error":
                raise RuntimeError(str(event.data.get("message", "Agent failed")))
        print()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it with `REPUBLIC_OPENAI_API_KEY=... uv run python sdk_example.py`. Running it again with the same `tape`
continues the conversation stored under `sessions/`.

| `Agent(...)` parameter | Behavior                                                                                                         |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `store`                | A `TapeStore` or `AsyncTapeStore`. Defaults to `InMemoryTapeStore()`.                                           |
| `tools`                | `Tool` objects the model may call, kept by name in `agent.tools`.                                                |
| `system_prompt`        | One system prompt section or a list of them; see [System prompt](#system-prompt).                                |
| `context`              | A `TapeContext` choosing which tape entries become model context. Defaults to `default_tape_context()`.          |
| `sidecars`             | `TapeSidecar` capabilities mounted on every tape.                                                                |
| `max_steps`            | Maximum model calls in one run.                                                                                  |
| `name`                 | Agent name reported in `invoke_agent` traces. Defaults to `"agent"`.                                             |

## Models

A run calls the model it is given, so one agent can serve runs with different models. Pass a Republic chat model
from `republic.get_model()`; its lifecycle belongs to you, so open it with `async with` (or close it) around the
runs that use it. Credentials and endpoints are Republic client options:

```python
model = republic.get_model("openai:gpt-5-mini", api_key="...", api_base="https://example.test/v1")
stream = await agent.run(prompt, model=model, options={"max_tokens": 4096, "temperature": 0.2}, tape="t")
```

`options` are `republic.ChatOptions` sent with every model call of the run; the loop adds the step's tools.
A `BeforeLlmCall` handler may redirect a call to another `provider:model` spec: a model of the same provider reuses
the run model's client, and another provider gets a client configured from Republic's environment.

For a policy of your own, such as fallbacks, per-provider credentials, or a timeout for the whole call, pass an
object implementing `bub.core.ModelClient` instead: a `spec` (the default `provider:model`), a default
`max_tokens` (or `None`), and `open_stream(messages, spec=..., options=...)`, an async context manager yielding the
provider name and a `republic.Stream`. Bub's own `bub.builtin.model.BubModel` is such a client.

## Tools

`Tool.from_callable()` derives a schema from annotations and a description from the docstring, without registering
the tool globally. The `@tool` decorator from `bub.core` builds the same `Tool` and also adds it to Bub's global
registry, which Bub's own agent snapshots.

A tool created with `context=True` receives a `ToolContext` with the run's `tape`, turn `state`, `run_id`, and event
bus:

```python
from bub.core import ToolContext


def remember(note: str, *, context: ToolContext) -> str:
    """Remember a note for this conversation."""
    context.state.setdefault("notes", []).append(note)
    return "noted"


agent.tools["remember"] = Tool.from_callable(remember, context=True)
```

Tool errors are returned to the model as tool results rather than ending the run. Tool names containing `.` are
offered to the model with `_` instead (for example, `fs.read` becomes `fs_read`).

## System prompt

The system prompt is an ordered list of sections, rendered for every model step and joined by blank lines.
A section is text, or a sync or async function of a `PromptContext` returning text; an empty result or `None`
skips the section. `agent.add_system_prompt(section)` appends a section and returns it, so it also works as a
decorator; `agent.remove_system_prompt(section)` removes it.

```python
from bub.core import PromptContext


@agent.add_system_prompt
def tool_list(context: PromptContext) -> str:
    names = ", ".join(tool.name for tool in context.tools)
    return f"Tools available in this step: {names}."


@agent.add_system_prompt
async def customer(context: PromptContext) -> str | None:
    if (user := context.state.get("user")) is None:
        return None
    return f"You are talking to {user}."
```

The context carries the step's `prompt` (empty when the step continues after tool calls), the model-facing `tools`
of the step, the `provider:model` spec of the step's `model`, the run's `tape` and `state`, and the `agent` (with an
`available_tools` shortcut).

## Sessions and tapes

Each run works on the tape named by `tape=` in the agent's store. Reuse a name to continue a conversation; choose
a new one for a new conversation. The run writes to a fork of the tape that merges back when the run's stream
closes; pass `merge_back=False` to discard it.

`state=` is the run's mutable turn state: a dictionary shared with system prompt sections, tools, and event
handlers. The agent does not persist it; keep anything that must survive in your application or on the tape.

```python
stream = await agent.run(["Any news on A123?"], model=model, tape="customer-42", state={"user": "Ada"})

tape = agent.tape("customer-42")
messages = await tape.read_messages()  # The conversation as Republic messages.
await tape.handoff(name="summary", state={"summary": "Ada asked about order A123."})
```

`agent.tape(name)` returns a conversation's tape for use outside runs; it writes to the store directly. By default the
model context is the chat recorded since the tape's last anchor, so a `handoff()` starts a shorter context. When a
call fails because the context is too long, the run hands off automatically once and retries the prompt.

## Events

The loop reports its progress as events from `bub.core.events`; register handlers to observe or steer it.

```python
from bub.core import AfterToolCall, BeforeToolCall, StepFinished, ToolCallDecision


@agent.events.on(BeforeToolCall)
def guard(event: BeforeToolCall) -> None:
    if event.call.tool == "lookup_order" and not event.call.arguments.get("order_id"):
        event.decision = ToolCallDecision.deny("order_id is required")


@agent.events.on(AfterToolCall)
async def audit(event: AfterToolCall) -> None:
    print("tool", event.call.tool, event.result.duration_ms, "ms")


agent.events.on(StepFinished, lambda event: print("step", event.step, event.status))
```

| Event                          | When                                                     | Handlers may                                                                    |
| ------------------------------ | -------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `RunStarted`                   | A run begins                                             | observe the model spec, prompt, and run metadata                                |
| `StepStarted` / `StepFinished` | A model step begins / ends with a status                 | observe                                                                         |
| `BeforeLlmCall`                | Before each model call                                   | replace `event.request` (model, messages, `max_tokens`), or set `event.decision = LlmCallDecision.finish(text)` to skip the call |
| `AfterLlmCall`                 | After each completed or failed model call                | observe text, tool calls, usage, and errors                                     |
| `BeforeToolCall`               | Before each tool call                                    | replace `event.call` to change arguments, or set `ToolCallDecision.replace(result)` / `deny(message)` |
| `AfterToolCall`                | After each tool call, including denied and replaced ones | assign `event.result.result` to change what the model sees                      |
| `ChatCompleted`                | A step's exchange is ready to record                     | observe                                                                         |

`agent.events.on(EventType, handler)` registers a sync or async handler and returns it; `@agent.events.on(EventType)`
does the same as a decorator, and `agent.events.off(EventType, handler)` removes it. A handler for `AgentEvent` receives every event.
Handlers run in registration order. Setting a decision on a `Before*` event stops delivery to later handlers.
Handler exceptions propagate to the run.

Every event carries the run's `state`; loop events also carry its `tape`. Recording is itself a handler: a
`TapeRecorder`, registered first, writes the conversation and `loop.*` events to the run's tape.

### Custom events

Define your own events to connect tools, handlers, and your application through the same bus. An event is a
keyword-only dataclass subclassing `AgentEvent` (or `TapeEvent`, which also carries the run's `tape`). Emit it with
`await events.emit(event)`, which delivers it to the handlers registered for its type or a base type and returns it.
Inside a tool, the run's bus is `context.events`:

```python
from dataclasses import dataclass

from bub.core import AgentEvent, ToolContext


@dataclass(kw_only=True)
class OrderLookedUp(AgentEvent):
    order_id: str
    status: str


@dataclass(kw_only=True)
class RefundRequested(AgentEvent):
    order_id: str
    approved: bool | None = None

    @property
    def stopped(self) -> bool:
        # Once a handler decides, later handlers are skipped.
        return self.approved is not None


async def lookup_order(order_id: str, *, context: ToolContext) -> dict[str, str]:
    """Look up the status of an order and whether it can be refunded."""
    result = {"order_id": order_id, "status": "shipped"}
    if context.events is not None:  # None when the tool runs outside an agent.
        await context.events.emit(OrderLookedUp(state=context.state, **result))
        request = await context.events.emit(RefundRequested(state=context.state, order_id=order_id))
        result["refundable"] = str(bool(request.approved))
    return result


agent.tools["lookup_order"] = Tool.from_callable(lookup_order, context=True)


@agent.events.on(OrderLookedUp)
async def notify(event: OrderLookedUp) -> None:
    print("order", event.order_id, "is", event.status, "for", event.state.get("user"))


@agent.events.on(RefundRequested)
def approve(event: RefundRequested) -> None:
    event.approved = event.state.get("user") == "Ada"
```

Every event carries the `state` of the run that emits it, so pass `context.state` (or `event.state` from another
handler). Handlers may modify a mutable event, and the emitter reads the result from the returned event; override
`stopped` to end delivery once a handler has decided. Your application can also emit events directly with
`await agent.events.emit(...)`. Custom events are not written to the tape; to record one, append it from a handler,
for example `await context.tape.append_event("order.looked_up", {...})` in the tool, or use a `TapeEvent` and call
`event.tape.append_event(...)` in its handler.

## Streams and lifecycle

`run()` is awaited first, then its result is iterated. Events include `text`, `reasoning`, `tool_call`,
`tool_result`, `usage`, `error`, and `final`. A `final` event finishes a model step; exhaust the iterator to finish
the whole run, including merging its tape fork. Check error events and handle exceptions from iteration. The stream
also exposes `error` and `usage`.

If you may stop consuming early, close the stream with `aclosing` so its model stream, tape fork, and trace span close
promptly. Closing works before iteration starts too:

```python
from contextlib import aclosing

stream = await agent.run(["Check order A123"], model=model, tape="customer-42")
async with aclosing(stream):
    async for event in stream:
        print(event.kind, event.data)
```

An empty prompt yields an `error: empty prompt` result without opening the tape. Runs are traced as
`invoke_agent {name}` spans when tracing is configured; see [Observability](/docs/tutorials/observability/).

To drive the loop on a tape you manage yourself, open it with `agent.session(name, state=...)` and call
`agent.run_on(tape, prompt, model=model)`, or `agent.run_once(prompt, model=model, tape=tape)` for a single model
step. Both also take `options`.

## Customize the loop

Subclass `Agent` to change how each step is prepared. Each method receives the run's tape, whose `context.state` is
the turn state:

| Method                                 | Default   | Use                                                       |
| -------------------------------------- | --------- | --------------------------------------------------------- |
| `prepare_tools(tape, prompt)`          | all tools | Choose the model-facing tools for the next step.          |
| `pending_messages(tape)`               | none      | Return user messages that arrived during the run.         |
| `has_pending_messages(tape)`           | `False`   | Continue the run for another step while messages wait.    |
| `continue_prompt(tape, prompt, state)` | `None`    | Return a user message for the next step after tool calls. |

```python
from bub.core import Tape


class TriageAgent(Agent):
    async def prepare_tools(self, tape: Tape, prompt):
        tools = await super().prepare_tools(tape, prompt)
        if tape.context.state.get("read_only"):
            tools = [tool for tool in tools if tool.name == "lookup_order"]
        return tools
```

## Embed in FastAPI

Install the server dependencies with `uv add fastapi uvicorn`, then save this as `app.py` next to `sdk_example.py`:

```python
import asyncio
from contextlib import asynccontextmanager

import republic
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from sdk_example import create_agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with republic.get_model("openai:gpt-5-mini") as model:
        app.state.agent = create_agent()
        app.state.model = model
        app.state.turn_lock = asyncio.Lock()
        yield


app = FastAPI(lifespan=lifespan)


class Task(BaseModel):
    session_id: str
    prompt: str


@app.post("/tasks")
async def run_task(task: Task, request: Request):
    state = request.app.state
    async with state.turn_lock:
        stream = await state.agent.run([task.prompt], model=state.model, tape=task.session_id)
        parts: list[str] = []
        error: str | None = None
        async for event in stream:
            if event.kind == "text":
                parts.append(str(event.data.get("delta", "")))
            elif event.kind == "error":
                error = str(event.data.get("message", "Agent failed"))
        if error is not None:
            raise HTTPException(status_code=502, detail=error)
        return {"session_id": task.session_id, "output": "".join(parts)}
```

Start with `uv run uvicorn app:app`. This example serializes all runs in one process. The agent itself keeps no
per-run state, so concurrent runs on different tapes are safe; serialize runs on the same tape with
application-owned per-session locking. Multiple worker processes sharing a store need coordination across workers;
a file write lock does not serialize a run.

## Bub's agent

`bub.builtin.BubAgent` is the core `Agent` configured the way Bub runs it: the model comes from Bub settings, system
prompt and interception [hooks](/docs/build/hooks/) come from the framework's plugins, and it adds skills, commands,
code mode, deferred tools, and steering. Use it when you want Bub's behavior rather than your own assembly:

```python
from pathlib import Path

from bub import BubFramework
from bub.builtin import BubAgent
from bub.core.store import FileTapeStore

framework = BubFramework()
framework.load_builtin_hooks()  # Or load_hooks() to include installed plugins.
async with framework.running():
    agent = BubAgent(framework, tape_store=FileTapeStore(Path("sessions")), skill_dirs=[Path("skills")])
    stream = await agent.run_stream(
        session_id="customer-42",
        prompt="Use $order-policy to check order A123.",
        allowed_skills=["order-policy"],
    )
    async for event in stream:
        ...
```

Configure the model through `BUB_MODEL`, `BUB_API_KEY`, and optionally `BUB_API_BASE`, or a configuration file; see
[Configuration](/docs/reference/settings/). `run_stream()` loads the session's turn state through the framework,
applies per-turn options, and then calls the core `run()` on the session's tape:

- `tools=None` snapshots the global tool registry; `skill_dirs=None` searches project, user, and builtin skill roots.
- `tape_store=None` uses the framework's store, which exists inside `framework.running()`; create the agent there.
- `model` and `reasoning_effort` override the session's saved settings for one turn. `allowed_tools` and
  `allowed_skills` narrow the agent's tools and skills for one turn.
- A `session_id` starting with `temp/` runs on a fork that is not merged back.
- A text prompt starting with the command prefix (`,` by default, or `command_prefix=`) runs a command: known
  commands call the agent's tools directly, others run through its `bash` tool when available.

`BubAgent` runs your sections and handlers after Bub's own, so `agent.add_system_prompt()` and `agent.events.on()`
work the same. It passes each turn a `BubModel` built from settings: the configured or session model, its
fallbacks and credentials, and the session's reasoning effort. For the complete message pipeline, including `build_prompt`, `save_state`, outbound rendering, and channel
dispatch, use `BubFramework.process_inbound()`, which returns a `TurnResult`.
