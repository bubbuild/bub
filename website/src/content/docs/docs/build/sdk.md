---
title: Build your own agent
description: Choose a model, add tools, and run a Bub agent inside your Python program.
sidebar:
  order: 0
---

Choose a model, add tools, and connect the agent to your program. `bub.core.Agent` runs the model and tool loop while you decide how to store conversations and manage the surrounding application.

You supply the agent's components directly. The [Bub application](/docs/build/) builds on the same core and adds configuration and plugin discovery. You can also [reuse application plugins](/docs/build/reuse-plugins/) in your own service while keeping its existing entry points and calling code.

## Install

Use Python 3.12+ and add Bub to your project:

```bash
uv init my-bub
cd my-bub
uv add bub
```

The base `bub` package installs the dependencies needed by the agent. Add `bub[app]` when you want to use the Bub application's CLI, plugins, or channels. See [Installation](/docs/getting-started/install/) for other setup options.

<a id="create-an-agent"></a>

## Run your first agent

Save this as `sdk_example.py` in your project:

```python
import asyncio
from pathlib import Path

import republic

from bub.core import Agent
from bub.core.store import FileTapeStore
from bub.core.tools import Tool


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up the status of an order."""
    return {"order_id": order_id, "status": "shipped"}


async def main() -> None:
    agent = Agent(
        store=FileTapeStore(Path(__file__).resolve().parent / "sessions"),
        tools=[Tool.from_callable(lookup_order)],
        system_prompt="Use lookup_order to check order status before answering.",
    )
    async with republic.get_model("openai:gpt-5-mini") as model:
        async with agent.run(
            "Where is order A123?",
            model=model,
            options={"max_tokens": 4096},
            tape="customer-42",
        ) as stream:
            async for event in stream:
                if event.kind == "text":
                    print(event.data.get("delta", ""), end="", flush=True)
                elif event.kind == "error":
                    raise RuntimeError(str(event.data.get("message", "Agent failed")))
        print()


if __name__ == "__main__":
    asyncio.run(main())
```

Set `REPUBLIC_OPENAI_API_KEY` in your shell, then run:

```bash
uv run python sdk_example.py
```

The tool returns a demonstration order status. Replace its body with your own order service when you use it in an application. The agent gives the tool result back to the model, which writes the answer.

Run the program again with the same `tape` name to continue the conversation stored under `sessions/`. Change the name to start a separate conversation.

<a id="streams-and-lifecycle"></a>

## Manage the run and its stream

`agent.run()` yields a stream. Iterating the stream drives the loop: call the model, run its requested tools, then call the model again until it answers without further tool calls. A `final` event ends one model step; exhaust the stream to finish the run.

Keep the stream inside `async with`. Leaving the block closes it after normal completion, an exception, or an early `break`. Closing also releases the open model stream and merges the run's tape fork. Records already written are kept even when the run fails; see [Forks and partial results](/docs/concepts/tape-and-context/#forks).

If another component will consume the stream, `await agent.start(...)` returns it with the same arguments. That component must close it, including when it never starts iteration. Prefer `run()` when you own the consuming code.

Stream events include `text`, `reasoning`, `tool_call`, `tool_result`, `usage`, `error`, and `final`. Handle error events and exceptions from iteration. For internal lifecycle notifications and call interception, use [agent events](/docs/concepts/events/).

<a id="models"></a>

## Choose a model

Pass a Republic chat model to `Agent(model=...)` for a default, or to `agent.run(model=...)` for one run. A run that needs a model raises `ValueError` when neither is supplied. You own the model's lifetime, so open it with `async with` around the runs that use it.

Republic accepts provider credentials and endpoints directly:

```python
async with republic.get_model(
    "openai:gpt-5-mini", api_key="...", api_base="https://example.test/v1"
) as model:
    agent = Agent(model=model)
    async with agent.run("Hello", tape="example", options={"max_tokens": 4096}) as stream:
        async for event in stream:
            print(event.kind, event.data)
```

`options` are Republic chat options sent with each model call of the run. The agent adds the tools for that step. Configure this client through Republic's arguments or environment variables; `BUB_*` settings belong to the Bub application.

For your own fallback, credential, or timeout policy, implement `bub.core.ModelClient`:

| Member | Contract |
| --- | --- |
| `spec` | Default `provider:model` string. |
| `max_tokens` | Default output token limit, or `None`. |
| `open_stream(messages, *, spec, options)` | Async context manager yielding `(provider_name, republic_stream)`. Messages are `republic.Message` objects; options include the step's tools. |

A `LLMCallStarted` handler can change the requested model. With a Republic chat model, a change within the same provider reuses its client; a different provider uses a client configured from Republic's environment. Supply a `ModelClient` when this policy needs to differ.

<a id="system-prompt"></a>

## Set the instructions

`system_prompt` accepts text, a function, or an ordered list of either. Functions receive a `PromptContext` and may be synchronous or asynchronous. The agent renders the sections before each model step, omits empty results, and joins the rest with blank lines.

```python
from bub.core import PromptContext


@agent.system_prompt.add
def customer(context: PromptContext) -> str | None:
    if (name := context.state.get("user")) is not None:
        return f"You are helping {name}."
    return None
```

`PromptContext` carries the step's prompt, model-facing tools, model spec, tape, turn state, and agent. `available_tools` lists the agent's registered tools. Remove a section with `agent.system_prompt.remove(section)`.

<a id="tapes-and-state"></a>
<a id="sessions-and-tapes"></a>

The `state` passed to a run is a mutable dictionary shared by tools, prompt sections, and event handlers. You decide which values to save and how to restore them for another run. See [Turn state](/docs/concepts/tape-and-context/#state) for what to keep between runs.

<a id="tools"></a>

<a id="agent-parameters"></a>

## Configure the agent

| Parameter | Default and purpose |
| --- | --- |
| `model` | `None`. Default Republic chat model or `ModelClient`. |
| `store` | A new `InMemoryTapeStore`. Accepts synchronous or asynchronous tape stores. |
| `tools` | Empty. Accepts `Tool` objects and Republic provider tools such as `republic.tools.WebSearch()`. |
| `system_prompt` | Empty. Text or callable sections, rendered per step. |
| `context` | `default_tape_context()`. Selects tape entries for model messages. |
| `sidecars` | Empty. Extra storage capabilities mounted alongside the tape. |
| `max_steps` | `sys.maxsize`. Maximum model steps in a run. |
| `max_tool_concurrency` | `None`, so calls from one model response may all run concurrently. Set `1` to run them sequentially. Your application coordinates separate runs. |
| `name` | `"agent"`. Name used in agent trace spans. |
| `command_prefix` | `None`. Set a prefix to invoke tools through text commands without a model. |

The agent keeps supplied `Tool` objects in `agent.tools` by name and provider tools in `agent.builtin_tools`. You select its tools explicitly when constructing or updating the agent.

<a id="customize-the-loop"></a>

## Customize how a step is prepared

Subclass `Agent` when selecting tools or accepting messages needs application logic. Each method receives the active tape; its `context.state` is the run's state.

| Method | Default behavior |
| --- | --- |
| `prepare_tools(tape, prompt)` | Offers supplied tools except those marked `exposure="command"`. |
| `pending_messages(tape)` | Returns no additional user messages. |
| `has_pending_messages(tape)` | Returns `False`. |
| `continue_prompt(tape, prompt, state)` | Returns `None`; continues after tool calls without an additional user message. Here `state` is the stream state. |

For a tape whose lifecycle you manage, open `agent.session(name, state=...)` and use `agent.run_on(tape, prompt, model=...)`. It returns a stream you must close. `agent.run_once(prompt, model=..., tape=...)` yields one model step when you need to drive the loop yourself.

<a id="events"></a>
<a id="commands"></a>

## Keep building

- [Add tools and CodeMode](/docs/build/tools/) to give your agent actions.
- [Store conversation history](/docs/concepts/tape-and-context/) to continue work across runs.
- [Serve your agent with FastAPI](/docs/tutorials/fastapi/) when another program needs an HTTP endpoint.
