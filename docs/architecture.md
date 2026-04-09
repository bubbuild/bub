# Architecture

## Core Components

- `BubFramework`: creates the plugin manager, loads plugins, and runs `process_inbound()`.
- `BubHookSpecs`: defines all hook contracts (`src/bub/hookspecs.py`).
- `HookRuntime`: executes hooks with sync/async compatibility helpers (`src/bub/hook_runtime.py`).
- `Agent`: builtin model-and-tools runtime (`src/bub/builtin/agent.py`).
- `ChannelManager`: starts channels, buffers inbound messages, and routes outbound messages (`src/bub/channels/manager.py`).

## Turn Lifecycle

`BubFramework.process_inbound()` currently executes in this order:

1. Resolve session via `resolve_session(message)` (fallback to `channel:chat_id` if empty).
2. Initialize state with `_runtime_workspace` from `BubFramework.workspace`.
3. Merge all `load_state(message, session_id)` dicts.
4. Build prompt via `build_prompt(message, session_id, state)` (fallback to inbound `content` if empty).
5. Execute `run_model_stream(prompt, session_id, state)`.
6. For each stream event, call `OutboundChannelRouter.dispatch_event(...)`, which forwards to `channel.on_event(event, message)` when the target channel exists.
7. Always execute `save_state(...)` in a `finally` block.
8. Render outbound batches via `render_outbound(...)`, then flatten them.
9. If no outbound exists, emit one fallback outbound.
10. Dispatch each outbound via `dispatch_outbound(message)`.

If no plugin implements `run_model_stream`, `HookRuntime` falls back to `run_model(prompt, session_id, state)` and adapts the returned text into a stream with a single text chunk.

## Hook Priority Semantics

- Registration order:
1. Builtin plugin `builtin`
2. External entry points (`group="bub"`)
- Execution order:
1. `HookRuntime` reverses pluggy implementation order, so later-registered plugins run first.
2. `call_first` returns the first non-`None` value.
3. `call_many` collects every implementation return value (including `None`).
- Merge/override details:
1. `load_state` is reversed again before merge so high-priority plugins win on key collisions.
2. `provide_channels` is collected by `BubFramework.get_channels()`, and the first channel name wins, so high-priority plugins can override builtin channel names.

## Error Behavior

- For normal hooks, `HookRuntime` does not swallow implementation errors.
- `process_inbound()` catches top-level exceptions, notifies `on_error(stage="turn", ...)`, then re-raises.
- `on_error` itself is observer-safe: one failing observer does not block the others.
- In sync calls (`call_first_sync`/`call_many_sync`), awaitable return values are skipped with a warning.

## Builtin Runtime Notes

Builtin `BuiltinImpl` behavior includes:

- `build_prompt`: supports comma command mode; non-command text may include `context_str`.
- `run_model_stream`: delegates to `Agent.run()`.
- `system_prompt`: combines a default prompt with workspace `AGENTS.md`.
- `register_cli_commands`: installs `run`, `gateway`, `chat`, plus hidden diagnostic commands.
- `provide_channels`: returns `telegram` and `cli` channel adapters.
- `provide_tape_store`: returns a file-backed tape store under `~/.bub/tapes`.

## Channel Event Streaming

Channels have two different outbound surfaces:

- `send(message)`: handles the final rendered outbound message.
- `on_event(event, message)`: handles raw stream events while the model is still running.

`on_event` is optional. Implement it when a channel can benefit from incremental rendering, typing indicators, progress updates, or partial text display. The `message` argument is the original inbound message, so channel implementations usually use it to recover routing metadata such as target channel, chat id, session id, or message kind.

If a channel does not implement any special event behavior, it can ignore `on_event` and rely entirely on `send()`.

## Boundaries

- `Envelope` stays intentionally weakly typed (`Any` + accessor helpers).
- There is no globally enforced schema for cross-plugin `state`.
- Runtime behavior in this document is aligned with current source code.
