# CLI

`bub` exposes four main commands (`run`, `gateway`, `chat`, `login`) plus one hidden one (`hooks` for diagnostics).

## `bub run`

Run one inbound message through the full framework pipeline and print outbounds.

```bash
uv run bub run "hello" --channel cli --chat-id local
```

Common options:

- `--workspace/-w`: workspace root, declared once on the top-level CLI and shared by all subcommands
- `--channel`: source channel (default `cli`)
- `--chat-id`: source endpoint id (default `local`)
- `--sender-id`: sender identity (default `human`)
- `--session-id`: explicit session id (default is `<channel>:<chat_id>`)

Comma-prefixed input enters internal command mode:

```bash
uv run bub run ",help"
uv run bub run ",skill name=my-skill"
uv run bub run ",fs.read path=README.md"
```

Unknown comma commands fall back to shell execution:

```bash
uv run bub run ",echo hello-from-shell"
```

## `bub hooks`

Print hook-to-plugin bindings discovered at startup.

```bash
uv run bub hooks
```

`hooks` remains available for diagnostics, but it is hidden from the top-level help.

## `bub gateway`

Start channel listener mode (defaults to all non-`cli` channels).

```bash
uv run bub gateway
```

Enable only selected channels:

```bash
uv run bub gateway --enable-channel telegram
```

## `bub chat`

Start an interactive REPL session via the `cli` channel.

```bash
uv run bub chat
uv run bub chat --chat-id local --session-id cli:local
```

## `bub login`

Authenticate with OpenAI Codex OAuth and persist the resulting credentials under `CODEX_HOME` (default `~/.codex`).

```bash
uv run bub login openai
```

Manual callback mode is useful when the local redirect server is unavailable:

```bash
uv run bub login openai --manual --no-browser
```

After login, you can use an OpenAI model without setting `BUB_API_KEY`:

```bash
BUB_MODEL=openai:gpt-5-codex uv run bub chat
```

If the upstream endpoint expects a specific request payload format, set `BUB_API_FORMAT`:

- `completion`: legacy completion-style format; default
- `responses`: OpenAI Responses API format
- `messages`: Anthropic Messages API format

```bash
BUB_MODEL=openai:gpt-5-codex BUB_API_FORMAT=responses uv run bub chat
```

## Notes

- `--workspace` is parsed before the subcommand, for example `uv run bub --workspace /repo chat`.
- `run` prints each outbound as:

```text
[channel:chat_id]
content
```
