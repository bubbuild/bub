# Channels

Bub uses channel adapters to run the same pipeline across different I/O endpoints. Hooks don't know which channel they're in.

## Builtin Channels

- `cli`: local interactive terminal — see [CLI](cli.md)
- `telegram`: Telegram bot — see [Telegram](telegram.md)

## Run Modes

Local interactive mode:

```bash
uv run bub chat
```

Channel listener mode (all non-`cli` channels by default):

```bash
uv run bub gateway
```

Enable only Telegram:

```bash
uv run bub gateway --enable-channel telegram
```

## Session Semantics

- `run` command default session id: `<channel>:<chat_id>`
- Telegram channel session id: `telegram:<chat_id>`
- `chat` command default session id: `cli_session` (override with `--session-id`)
- `chat` command default chat id: `local`

## Outbound Delivery Surfaces

Channel adapters can receive outbound data in two forms:

- `send(message)`: the final rendered outbound message
- `on_event(event, message)`: streaming events emitted while the model is still producing output

Use `on_event` for incremental UX such as live text updates, typing indicators, progress bars, or chunk-level logging. Use `send` for the final durable outbound payload.

`on_event` is optional. A channel that does not need streaming behavior can ignore it and only implement `send`.

## Debounce Behavior

- `cli` does not debounce; each input is processed immediately.
- Other channels can debounce and batch inbound messages per session.
- Comma commands (`,` prefix) always bypass debounce and execute immediately.

## About Discord

Core Bub does not currently include a builtin Discord adapter.
If you need Discord, implement it in an external plugin via `provide_channels`.
