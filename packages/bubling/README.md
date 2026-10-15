# bubling

The event-driven agent kernel of [Bub](https://bub.build). Its `Agent` runs the agent loop (call the model,
execute the tools it asks for, repeat until it answers) over components you supply: tools, system prompt
sections, and a tape store, plus the [Republic](https://github.com/bubbuild/republic) model it calls.

It reads no settings and loads no plugins, so it embeds in any application:

```bash
uv add bubling
```

```python
import asyncio

import republic

from bubling import Agent, Tool


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up the status of an order."""
    return {"order_id": order_id, "status": "shipped"}


async def main() -> None:
    agent = Agent(tools=[Tool.from_callable(lookup_order)], system_prompt=["You are an order assistant."])
    async with republic.get_model("openai:gpt-5-mini") as model:
        async with agent.run("Where is order A123?", model=model, tape="customer-42") as stream:
            async for event in stream:
                if event.kind == "text":
                    print(event.data.get("delta", ""), end="", flush=True)


asyncio.run(main())
```

See the [Python SDK guide](https://bub.build/docs/build/sdk/) for the full API. Install
[`bub`](https://pypi.org/project/bub/) for the complete runtime: CLI, channels, hooks, skills and the builtin
agent, built on this package.
