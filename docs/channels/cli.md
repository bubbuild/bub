# CLI

`bub` exposes six public command groups: `run`, `gateway`, `chat`, `install`, `update`, and `login`. It also keeps one hidden diagnostic command: `hooks`.

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

`chat` reuses the top-level `--workspace/-w` option. By default it sets `chat_id=local`, keeps the channel as `cli`, and leaves the underlying CLI channel session id at `cli_session` unless you pass `--session-id`.

## `bub install`

Install packages into Bub's managed plugin project, or sync that project if no package spec is given.

```bash
uv run bub install
uv run bub install my-plugin
uv run bub install example-owner/example-bub-plugin@main
uv run bub install https://github.com/example/bub-plugin.git
```

Options and behavior:

- `--project`: path to the uv project used for Bub-managed plugins
- `BUB_PROJECT`: environment variable equivalent of `--project`
- default project path: `~/.bub/bub-project`
- if the project does not exist yet, Bub creates a bare uv app project automatically
- Bub must be installed inside a virtual environment before `install` or `update` can run

Accepted spec forms:

- `https://...` or `git@...`: installed as a Git requirement
- `owner/repo` or `owner/repo@ref`: expanded to a GitHub repository URL
- `name`: passed through as a normal package requirement
- `name@ref`: resolved from `bub-contrib` as `packages/<name>` at the given Git ref

If no package specs are provided, `install` runs a project sync instead of adding anything new.

## `bub update`

Update dependencies inside Bub's managed plugin project.

```bash
uv run bub update
uv run bub update my-plugin another-plugin
```

Behavior:

- with no package names, upgrades the whole project
- with package names, upgrades only the selected dependencies
- uses the same `--project` / `BUB_PROJECT` location as `install`
- creates the managed project first if it does not exist yet

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
- `install` and `update` operate on Bub's managed plugin project, not on the current workspace.
- `uninstall()` exists in the builtin module but is not currently exposed as a public CLI command.
- `run` prints each outbound as:

```text
[channel:chat_id]
content
```
