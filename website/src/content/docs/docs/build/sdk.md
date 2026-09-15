---
title: Python SDK
description: Embed Bub in a Python application with custom tools, prompts, skills, and session storage.
sidebar:
  order: 0
---

Use `bub.builtin.Agent` to run the builtin agent loop inside your application.
`BubFramework` supplies configuration and hooks; `Agent` owns execution with your tools and store.

## Install and configure

Install Bub in your Python 3.12+ project:

```bash
uv add bub
```

Configure a model and its credentials through `BUB_MODEL`, `BUB_API_KEY`, and optionally `BUB_API_BASE`,
or through a configuration file. See [Configuration](/docs/reference/settings/).
The API below describes this source checkout; use a release containing these interfaces or install the checkout.

## Create an agent

Save the following as `sdk_example.py`:

```python
import asyncio
from pathlib import Path

from bub import BubFramework, hookimpl
from bub.builtin import Agent
from bub.builtin.hook_impl import BuiltinImpl
from bub.builtin.tools import skill_describe
from bub.store import FileTapeStore
from bub.tools import Tool


class ApplicationHooks(BuiltinImpl):
    def __init__(self, framework: BubFramework, prompts: list[str]) -> None:
        super().__init__(framework)
        self.prompts = tuple(prompts)

    @hookimpl
    def system_prompt(self, prompt, state) -> str:
        return "\n\n".join(self.prompts)


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up the status of an order."""
    # Replace this demonstration response with your application's order service.
    return {"order_id": order_id, "status": "shipped"}


def create_agent() -> tuple[BubFramework, Agent]:
    root = Path(__file__).resolve().parent
    framework = BubFramework(config_file=root / "config.yml")
    framework.workspace = root
    framework.plugin_manager.register(
        ApplicationHooks(framework, [
            "You are an order assistant. Reply directly to the user.",
            "Use lookup_order to check order status before answering.",
        ]),
        name="application",
    )
    agent = Agent(
        framework,
        tools=[Tool.from_callable(lookup_order), skill_describe],
        skill_dirs=[root / "skills"],
        tape_store=FileTapeStore(root / "sessions"),
    )
    return framework, agent


async def main() -> None:
    framework, agent = create_agent()
    async with framework.running():
        stream = await agent.run_stream(
            session_id="customer-42",
            prompt="Where is order A123?",
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

Run it with `uv run python sdk_example.py` after configuring your model.
An absent `config.yml` is allowed; environment settings still apply.

`ApplicationHooks` replaces the builtin system-prompt method while keeping the other builtin hooks.
Register this instance once: do not also call `load_builtin_hooks()` or `load_hooks()` in this example.
System-prompt hooks are additive, so registering an extra prompt hook alongside the standard builtin implementation
would retain its channel instructions and workspace `AGENTS.md` content.

For standard Bub defaults instead, create a framework and call `framework.load_builtin_hooks()`.
Use `load_hooks()` when you also want installed plugins from the `bub` entry-point group.

## Tools, skills, and sessions

| Parameter | Behavior |
| --- | --- |
| `tools=[...]` | Accepts `Tool` objects. `Tool.from_callable()` derives a schema from annotations and a description from the docstring. |
| `tools=None` | Copies the global tool registry at construction time. `tools=[]` disables tools. |
| `skill_dirs=[Path(...)]` | Searches only these roots, in order; the first skill of a given name wins. |
| `skill_dirs=None` | Searches project, user, and builtin roots. `skill_dirs=[]` disables discovery. |
| `tape_store=...` | Uses the supplied `TapeStore` or `AsyncTapeStore`. `FileTapeStore(path)` persists session tapes in that directory. |
| `tape_store=None` | Uses the framework's active store, or an instance-local memory store if no store is active. |

Unlike `@tool`, `Tool.from_callable()` does not register the tool globally.
Include `skill_describe` in the tool set when the model needs to load skill bodies on demand.
Skills supply instructions; they do not automatically grant shell or filesystem tools.

Each skill lives in its own directory:

```text
skills/
└── order-policy/
    └── SKILL.md
```

```markdown
---
name: order-policy
description: Rules for answering order-status questions.
---
Check the order service before giving a delivery status.
```

Reuse a `session_id` with the same workspace and store to continue a conversation.
Choose a new ID for a new conversation. IDs starting with `temp/` run on a fork that is not merged back.
Archive and builtin sidecar paths still follow Bub configuration, independently of `FileTapeStore`'s directory.

Per-turn options narrow the instance's capabilities and override persisted model settings:

```python
stream = await agent.run_stream(
    session_id="customer-42",
    prompt="Use $order-policy to check order A123.",
    allowed_tools=["lookup_order", "skill"],
    allowed_skills=["order-policy"],
    model="provider:model-id",  # Replace with your configured provider/model.
    reasoning_effort="high",  # Use a value supported by that model.
)
```

Always consume the returned stream. `allowed_tools` accepts runtime names and model aliases
(for example, `fs.read` and `fs_read`). Explicit `model` and `reasoning_effort` take precedence over saved settings.
Normally omit `state` to load session state automatically; passing a state dictionary skips that loading and mutates it.
Comma-prefixed text is still treated as a command. Command execution uses the instance tool set directly,
so loop-level `allowed_tools` filtering is not a command permission boundary.

## Streams and lifecycle

`run_stream()` is awaited first, then its result is iterated. Events include `text`, `reasoning`, `tool_call`,
`tool_result`, `usage`, `error`, and `final`. A `final` event finishes a model step; exhaust the iterator to finish
the whole turn. Check error events and handle exceptions from iteration. The stream also exposes `error` and `usage`.

For early exit from an iteration that has started, close its iterator explicitly:

```python
from contextlib import aclosing

stream = await agent.run_stream(session_id="customer-42", prompt="Check order A123")
async with aclosing(stream.__aiter__()) as events:
    async for event in events:
        print(event.kind, event.data)
```

Keep `framework.running()` open until all turns finish. Accessing the agent's cached `tape` before entering that
lifespan can bind it to an in-memory fallback. An explicitly injected store's lifecycle belongs to your application.
`Agent` itself is not an async context manager and has no `run()` convenience method.

## Embed in FastAPI

Install the server dependencies with `uv add fastapi uvicorn`, then save this as `app.py` next to `sdk_example.py`:

```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from sdk_example import create_agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    framework, agent = create_agent()
    async with framework.running():
        app.state.agent = agent
        app.state.turn_lock = asyncio.Lock()
        yield


app = FastAPI(lifespan=lifespan)


class Task(BaseModel):
    session_id: str
    prompt: str


@app.post("/tasks")
async def run_task(task: Task, request: Request):
    async with request.app.state.turn_lock:
        stream = await request.app.state.agent.run_stream(
            session_id=task.session_id,
            prompt=task.prompt,
        )
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

Start with `uv run uvicorn app:app`. This example serializes all turns in one process.
For concurrent independent sessions, use application-owned per-session locking with a cleanup policy.
Multiple worker processes sharing a store need coordination across workers; a file write lock does not serialize a turn.

## Agent versus the full message pipeline

Direct `Agent.run_stream()` uses the builtin loop and its model/tool interception hooks.
It does not run `build_prompt`, `save_state`, outbound rendering, or channel dispatch, and does not select a plugin's
replacement `run_model` implementation. Your application consumes the result.

Use `BubFramework.process_inbound()` for the complete message pipeline and model-runner plugins.
It returns a `TurnResult` with `model_output`, `state`, and `outbounds`; `stream_output=True` routes streaming output
through a bound channel router but still returns the completed result.
See [Hooks](/docs/build/hooks/) for those extension points.

Configuration loading remains process-wide. Configure once at application startup;
separate tool/skill/store instances do not imply isolated configuration files or environment variables.
