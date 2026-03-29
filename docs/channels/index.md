# Channels

Bub uses channel adapters to run the same pipeline across different I/O endpoints. Hooks don't know which channel they're in.

## Builtin Channels

- `cli`: local interactive terminal — see [CLI](cli.md)
- `telegram`: Telegram bot — see [Telegram](telegram.md)
- `wechat`: Tencent WeChat via the official iLink bot protocol — see [WeChat](wechat.md)

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

Enable only WeChat:

```bash
uv run bub gateway --enable-channel wechat
```

## Session Semantics

- `run` command default session id: `<channel>:<chat_id>`
- Telegram channel session id: `telegram:<chat_id>`
- WeChat channel session id: `wechat:<user_id>`
- `chat` command default session id: `cli_session` (override with `--session-id`)

## Debounce Behavior

- `cli` does not debounce; each input is processed immediately.
- Other channels can debounce and batch inbound messages per session.
- Comma commands (`,` prefix) always bypass debounce and execute immediately.

## About Discord

Core Bub does not currently include a builtin Discord adapter.
If you need Discord, implement it in an external plugin via `provide_channels`.
