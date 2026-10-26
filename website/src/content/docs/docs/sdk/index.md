---
title: Run an agent in Python
description: Run an Agent in a Python program, add tools, and manage sessions and output streams.
---

`bub.core.Agent` handles the loop between the model and its tools. You provide the model and tools, and decide how sessions are saved and how output connects to your existing system.

This page uses the base SDK installation and does not need the `app` optional dependency. See [Install the SDK](/docs/getting-started/install/#agent-install) for the command. When you need configuration, skills, channels, and plugin discovery, use the [Bub application](/docs/app/workspace/).

## Run an agent with a tool {#basic-agent}

Save the following code as `sdk_example.py`:

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

Set the OpenAI API key and run it:

```bash
export REPUBLIC_OPENAI_API_KEY=sk-...
uv run python sdk_example.py
```

`lookup_order` returns demonstration data; replace its body when connecting a real system. The Agent gives the tool result back to the model, which then writes the answer.

`FileTapeStore` saves records under `sessions/`. Using `tape="customer-42"` again continues that session; changing the name starts a different one. If you omit `store`, an in-memory store is used.

## Consume the output stream {#stream-lifecycle}

`agent.run()` is an async context manager that yields an `AsyncStreamEvents` once entered. **Iterating the stream is what drives the model and tool loop.** The run ends when the model no longer requests tools and there are no pending messages.

A `final` event only marks the end of one model Step. A Step that contains tool calls may be followed by another Step. When handling text output, keep iterating until the stream ends; do not stop after the first `final`.

`async with` closes the stream, releases the model connection, and merges the run's Tape fork on normal completion, on an exception, or on an early `break`. Closing early marks the unfinished Run as `cancelled`; records already written are kept by default. See [Tape forks](/docs/concepts/tape/#forks).

If another component owns output consumption, `await agent.start(...)` takes the same arguments and returns the stream. That receiver must close it, including when it has not started iterating. Prefer `run()` when you consume the stream yourself.

Stream events include:

| `kind` | Contents |
| --- | --- |
| `text` / `reasoning` | Text or reasoning deltas |
| `tool_call` / `tool_result` | Tool calls and results |
| `usage` | Model usage |
| `error` | Error payload |
| `final` | The current model Step ended |

Errors can surface as an `error` event, or be raised while iterating. To observe internal lifecycle notifications or intercept calls, use [Agent events](/docs/concepts/events/); do not rely only on the output stream.

## Next steps {#next-steps}

- [Configure the agent](/docs/sdk/options/): the model, the prompts, and run parameters.
- [Custom tools and code mode](/docs/sdk/tools/): give the agent actions to perform.
- [Reuse the Bub app](/docs/sdk/reuse-app/): connect plugin tools, `BubAgent`, or a complete Turn at the depth you need.
- [FastAPI service](/docs/sdk/web-service/): expose an HTTP endpoint with per-session history.
