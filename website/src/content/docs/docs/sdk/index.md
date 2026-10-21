---
title: Basic agent
description: Create a basic agent in your own Python program and control the stream it produces.
sidebar:
  order: 0
---

Choose a model, add tools, and connect the agent to your program. `bub.core.Agent` runs the model and tool loop while you decide how to store conversations and manage the surrounding application.

This page assumes Bub is installed for the agent path; see [Install Bub](/docs/getting-started/install/#agent-install). The [Bub application](/docs/app/workspace/) builds on the same core and adds configuration and plugin discovery.

<a id="create-an-agent"></a>

## Run the example

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

Keep the stream inside `async with`. Leaving the block closes it after normal completion, an exception, or an early `break`. Closing also releases the open model stream and merges the run's tape fork. Records already written are kept even when the run fails; see [Forks and partial results](/docs/concepts/tape/#forks).

If another component will consume the stream, `await agent.start(...)` takes the same arguments and returns the stream. That component must close it, including when it never starts iteration. Prefer `run()` when you own the consuming code.

Stream events include `text`, `reasoning`, `tool_call`, `tool_result`, `usage`, `error`, and `final`. Handle error events and exceptions from iteration. For internal lifecycle notifications and call interception, use [agent events](/docs/concepts/events/).

## Next steps

- [Configure the agent](/docs/sdk/options/): the model, the instructions, and the rest of the `Agent` parameters.
- [Tools and CodeMode](/docs/sdk/tools/): give the agent actions.
- [Reusing plugins](/docs/sdk/reuse-plugins/): bring existing plugin tools and hooks into your agent.
- [Example: FastAPI service](/docs/sdk/web-service/): put the agent behind an HTTP endpoint.
