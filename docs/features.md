# Key Features

## Hook-First Runtime

Every turn stage is a [pluggy](https://pluggy.readthedocs.io/) hook.
Builtins are ordinary plugins — override any stage by registering your own.
Both first-result hooks (override) and broadcast hooks (observer) are supported.
`run_model` is the default model hook for turn execution.
`run_model_stream` remains available for incremental channel output, and either hook shape can be adapted to the other.
Safe fallback to prompt text when no model hook returns a value (with `on_error` notification).
Automatic fallback outbound when `render_outbound` produces nothing.

## Tape-Based Context

Runtime events are recorded to tapes (default under `~/.bub/tapes`).
Context is reconstructed from tape records, not accumulated in session state.

## Builtin Batteries

- **CLI**: `run`, `chat`, `gateway`, `install`, `update`, and `login` via Typer; hidden `hooks` remains available for diagnostics.
- **Model runtime**: agent loop with tool use, backed by [Republic](https://github.com/bubbuild/republic).
- **Comma commands**: `,help`, `,skill`, `,fs.read`, etc. Unknown commands fall back to shell.
- **Channels**: `cli` and `telegram` ship as defaults.
- **Streaming toggle**: channel event streaming is controlled by `BUB_STREAM_OUTPUT` and is off by default.

All of these are hook implementations. Replace what you need.

## Channel-Agnostic Pipeline

CLI and Telegram use the same `process_inbound()` path.
Hooks don't know which channel they're in.
Outbound routing is handled by `ChannelManager`.

## Skills

Skills are `SKILL.md` files with validated frontmatter.
Plugins can ship their own by including a `skills/` directory.

## Plugin Extensibility

External plugins are loaded via Python entry points (`group="bub"`).
Later-registered plugins run first and can override builtin behavior.

## Current Boundaries

- `Envelope` is intentionally weakly typed (`Any` + accessor helpers).
- No centralized key contract for shared plugin `state`.
- No builtin Discord adapter — implement one via `provide_channels`.
