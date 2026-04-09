# Extension Guide

This guide explains how to implement Bub hooks with `@hookimpl`, and how those implementations are executed in the current runtime.

## 1) Import And Basic Shape

Use the marker exported by Bub:

```python
from bub import hookimpl
```

Implement hooks on a plugin object:

```python
from __future__ import annotations

from bub import hookimpl


class MyPlugin:
    @hookimpl
    def build_prompt(self, message, session_id, state):
        return "custom prompt"

my_plugin = MyPlugin()
```

## 2) Register Plugin Via Entry Points

Expose your plugin in `pyproject.toml`:

```toml
[project.entry-points."bub"]
my_plugin = "my_package.plugin:my_plugin"
```

`BubFramework.load_hooks()` loads builtin first, then entry points in `group="bub"`.

## 3) Expose Tools By Importing The Module

Tools are registered through the `@tool` decorator's import-time side effect.
Your plugin must import the module that contains the `@tool` definitions before the agent starts using them.

Example:

```python
from __future__ import annotations

from bub import hookimpl

from . import tools  # noqa: F401


class MyPlugin:
    @hookimpl
    def system_prompt(self, prompt, state):
        return "extension prompt"
```

If that import is missing, the tool module never runs, nothing is inserted into `bub.tools.REGISTRY`, and the tool will not be available to the agent or CLI completion.

## 4) Ship Skills In Extension Packages

Extension packages can also ship skills by including a top-level `skills/` directory in the distribution.

Example layout:

```text
my-extension/
├─ src/
│  ├─ my_extension/
│  │  └─ plugin.py
│  └─ skills/
│     └─ my-skill/
│        └─ SKILL.md
└─ pyproject.toml
```

Configure your build backend to include the `skills/` directory in the package data. For example, with `pdm-backend`:

```toml
[tool.pdm.build]
includes = ["src/"]
```

At runtime, Bub discovers builtin skills from `<site-packages>/skills`, so packaged skills in that location are loaded automatically.
These skills use normal precedence rules and can still be overridden by workspace (`.agents/skills`) or user (`~/.agents/skills`) skills.

## 5) Hook Execution Semantics

`HookRuntime` drives most framework hooks:

- `call_first(...)`: execute by priority, return first non-`None`
- `call_many(...)`: execute all, collect all return values (including `None`)
- `call_first_sync(...)` / `call_many_sync(...)`: sync-only bootstrap paths

Current `process_inbound()` hook usage:

1. `resolve_session` (`call_first`)
2. `load_state` (`call_many`, then merged by framework)
3. `build_prompt` (`call_first`)
4. `run_model_stream` (`call_first`)
5. `save_state` (`call_many`, always executed in `finally`)
6. `render_outbound` (`call_many`)
7. `dispatch_outbound` (`call_many`, per outbound)

Compatibility note:

- `run_model_stream` is the primary model hook.
- If no plugin implements `run_model_stream`, Bub falls back to `run_model`.
- The `run_model` return value is wrapped into a stream with exactly one text chunk.
- A plugin should implement one of these hooks, not both.

Other hook consumers:

- `register_cli_commands`: called by `call_many_sync`
- `provide_channels`: called by `call_many_sync` in `BubFramework.get_channels()`
- `system_prompt`, `provide_tape_store`, `build_tape_context`: consumed by `BubFramework` and the builtin `Agent`

## 6) Priority And Override Rules

- Builtin plugin is registered first.
- Later plugins have higher runtime precedence.
- `HookRuntime` reverses pluggy implementation order so later registration runs first.
- For `load_state`, framework re-reverses before merge so high-priority values overwrite low-priority values.

## 7) Sync vs Async Rules

- Async hook calls can run both sync and async implementations.
- Sync hook calls skip awaitable return values and log a warning.
- Therefore, keep bootstrap hooks synchronous:
  - `register_cli_commands`
  - `provide_channels`
  - `provide_tape_store`
  - `build_tape_context`

## 8) Signature Matching

`HookRuntime` passes only parameters declared in your function signature.
You can safely omit unused hook arguments.

Example:

```python
from bub import hookimpl


class SessionPlugin:
    @hookimpl
    def resolve_session(self, message):
        return "my-session"
```

## 9) Minimal End-To-End Example

```python
from __future__ import annotations

from republic import AsyncStreamEvents, StreamEvent

from bub import hookimpl


class EchoPlugin:
    @hookimpl
    def build_prompt(self, message, session_id, state):
        return f"[echo] {message['content']}"

    @hookimpl
    async def run_model_stream(self, prompt, session_id, state):
        async def iterator():
            yield StreamEvent("text", {"delta": prompt})

        return AsyncStreamEvents(iterator())
```

Run and verify:

```bash
uv run bub hooks
uv run bub run "hello"
```

Check that your plugin is listed for `build_prompt` / `run_model_stream`, and output reflects your override.
If you intentionally use the legacy compatibility hook, check for `run_model`.

## 10) Listen To Parent Stream

If you want to observe or transform the parent stream instead of fully replacing it, implement `run_model_stream` and wrap the parent hook's async iterator.

This pattern uses `subset_hook_caller(...)` to call the same hook chain without the current plugin, then returns a new `AsyncStreamEvents` wrapper.

```python
from __future__ import annotations

from republic import AsyncStreamEvents, StreamEvent

from bub import hookimpl


class StreamTapPlugin:
    def __init__(self, framework) -> None:
        self.framework = framework

    @hookimpl
    async def run_model_stream(self, prompt, session_id, state):
        parent_hook = self.framework._plugin_manager.subset_hook_caller(
            "run_model_stream",
            remove_plugins=[self],
        )
        parent_stream = await parent_hook(
            prompt=prompt,
            session_id=session_id,
            state=state,
        )
        if parent_stream is None:
            raise RuntimeError("no parent run_model_stream implementation found")

        async def iterator():
            async for event in parent_stream:
                if event.kind == "text":
                    delta = str(event.data.get("delta", ""))
                    print(delta, end="")
                yield event

        return AsyncStreamEvents(iterator(), state=parent_stream._state)
```

Use this when you need to log chunks, redact text, inject extra events, or measure stream timing without reimplementing the underlying model call.

If you also need to support parents that only implement legacy `run_model`, add your own fallback path and wrap that text result into a one-chunk stream.

## 11) Common Pitfalls

- Defining `@tool` functions without importing the module from your plugin means the tools never register.
- Returning awaitables from hooks invoked via sync paths (`call_many_sync` / `call_first_sync`) causes skip.
- Assuming hook failures are isolated: non-`on_error` hook exceptions propagate and can fail the turn.
- Using stale hook names: always confirm against `src/bub/hookspecs.py`.
