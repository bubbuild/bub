---
title: The Agent Loop
description: Understand the core loop in bub.core.Agent, including Runs, Steps, stream events, and Tape memory management.
---

`bub.core.Agent` is the minimal core of `bub`. It drives the interaction loop between the model and your local tools, and you can embed it directly in any Python service, backend framework, or custom script.

In [Quickstart](/docs/getting-started/quickstart/), we already saw how to initialize and run an Agent:

```py
import asyncio
import republic
from bub.core import Agent


async def main() -> None:
    # Create the core Agent and give it a system prompt
    agent = Agent(system_prompt="Be concise.")

    # Use republic to get the underlying model connection
    async with republic.get_model("openai:gpt-5-mini") as model:
        # Start a Run and name the conversation Tape
        async with agent.run("What can you do?", model=model, tape="demo") as stream:
            async for event in stream:
                if event.kind == "text":
                    print(event.data.get("delta", ""), end="", flush=True)


asyncio.run(main())
```

This page looks at what happens inside `bub.core.Agent`.

## 1. Runs and Steps {#run-and-step}

Unlike a single API request, an Agent is fundamentally a **multi-step loop**. It sends user input to the model; when the model asks to call a tool, the Agent runs that tool locally and feeds the result back to the model, repeating until the model produces a final answer.

To make this process controllable, `bub` separates two levels in its architecture:

- **Run**: one complete user interaction, started by `agent.run(...)`.
- **Step**: one iteration inside a Run. **A Run contains one or more Steps.**

The diagram below shows a multi-step loop that calls a tool:

```mermaid
flowchart TD
    Start["Enter agent.run() and initialize a Tape fork"] --> Consume["Caller iterates the event stream with async for"]
    Start --> RunStarted["Emit RunStarted"]
    Consume --> Step["Start a Step: send context to the model"]
    Step --> Output["Yield reasoning / text / usage"]
    Output --> Decision{"Did the model request tools?"}
    Decision -->|"Yes"| Call["Yield tool_call and run the local tool"]
    Call --> Result["Yield tool_result and the Step's final"]
    Result --> Step
    Decision -->|"No"| Final["Yield the final text and a final event"]
    Final --> Close["Close the stream, emit RunFinished, merge the Tape fork"]
```

In the minimal example above, no tools are registered, so the model answers directly and the whole Run finishes in a single Step. Once you add tools, multiple Steps are chained automatically.

---

## 2. Consuming the stream {#consume-the-stream}

After you enter `agent.run()`, `bub` initializes the session and emits `RunStarted`. The model and tool loop is **lazy**: the Agent only starts stepping and working once you consume the event stream. Creating the context without consuming the stream does not run the task. A simple example:

```python
async with agent.run(prompt, model=model, tape="quickstart") as stream:
    async for event in stream:
        ...
```

Always put `agent.run()` inside an `async with` block. That guarantees the underlying connection and stream are cleaned up whether the Run completes normally, raises partway through, or is cancelled from outside. A Run that does not complete normally ends with the `cancelled` status.

You can read the type of every event from `event.kind`.

:::caution
A `final` event marks the end of the **current Step**, not the end of the Run.
If the model requested tools in this round, the Agent must run them and then enter another Step with the results, producing more events. So whether you are streaming a response to a client or tracking task completion, wait for the entire `async for event in stream` loop to finish before acting on it.
:::

## 3. Event types and payloads {#stream-events}

As you iterate over `stream`, you receive events with different `kind` values. Each event's `event.data` has the following shape:

| Event type | Example `event.data` | Meaning |
| --- | --- | --- |
| `reasoning` | `{"delta": "..."}` | Text deltas from the model's chain of thought or reasoning |
| `text` | `{"delta": "..."}` | Text deltas of the user-facing reply |
| `tool_call` | `{"tool_calls": [{"id", "name", "arguments"}, ...]}` | Tools the model asked to call (`arguments` is a JSON string) |
| `tool_result` | `{"tool_results": [...]}` | Execution results in call order; error details are also returned to the model here |
| `usage` | `{"usage": {...}, "elapsed_seconds": ...}` | Token usage and elapsed time for the current Step |
| `final` | `{"ok": True, "text": ...}` or a tool summary | Summary of the current Step |

A host program typically branches on `event.kind` as events arrive. That is why this is called the Agent loop: it is really one loop that dispatches a stream of events:

```python
async for event in stream:
    if event.kind == "tool_call":
        for call in event.data["tool_calls"]:
            print(f"[{event.step_index}] Model requested tool: {call['name']}")
    elif event.kind == "text":
        print(event.data["delta"], end="", flush=True)
```

## 4. Tape and memory branches {#tape-management}

You may have noticed the `tape="demo"` argument in the first example.

In Bub's architecture, a **Tape** is the core store for conversation history and state. It can:

- **Rebuild context from its records**: unlike traditional in-memory session state, which can hallucinate once the model context grows too large or is lost, `bub` rebuilds context on demand from an append-only Tape.
- **Branch**: `tape="demo"` attaches this Run to a specific Tape branch. When the stream closes normally, the branch's records merge back into the main Tape by default; if you configure the Run to discard branch records, they do not pollute the main history.

The Tape is what gives the Agent loop memory and multi-turn continuity. We cover it in detail in [Tape & Memory](/docs/sdk/tape/#forks-and-merges).
