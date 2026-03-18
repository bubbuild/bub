# Bub

**A common shape for agents that live alongside people.**

Bub started in group chats. Not as a demo or a personal assistant, but as a teammate that had to coexist with real humans and other agents in the same messy conversations — concurrent tasks, incomplete context, and nobody waiting.

It is hook-first, built on [pluggy](https://pluggy.readthedocs.io/), with a small core (~200 lines) and builtins that are just default plugins you can replace. Context comes from [tape](https://tape.systems), not session accumulation. The same pipeline runs across CLI, Telegram, and any channel you add.

[Website](https://bub.build) · [GitHub](https://github.com/bubbuild/bub)

## Quick Start

```bash
pip install bub
```

Or from source:

```bash
git clone https://github.com/bubbuild/bub.git
cd bub
uv sync
```

```bash
uv run bub chat                         # interactive session
uv run bub run "summarize this repo"    # one-shot task
uv run bub gateway                      # channel listener mode
```

## How It Works

Every inbound message goes through one turn pipeline. Each stage is a hook.

```
resolve_session → load_state → build_prompt → run_model
                                                   ↓
              dispatch_outbound ← render_outbound ← save_state
```

Builtins are plugins registered first. Later plugins override earlier ones. No special cases.

If `AGENTS.md` exists in the workspace, it is appended to the system prompt automatically.

Key source files:

- Turn orchestrator: [`src/bub/framework.py`](src/bub/framework.py)
- Hook contract: [`src/bub/hookspecs.py`](src/bub/hookspecs.py)
- Builtin hooks: [`src/bub/builtin/hook_impl.py`](src/bub/builtin/hook_impl.py)
- Skill discovery: [`src/bub/skills.py`](src/bub/skills.py)

## What Sets It Apart

Bub grew up in multi-person chats with multiple agents running at the same time. Single-user flows hide structural problems; shared environments expose them fast. That shaped a few things:

- **Context from tape.** History is append-only facts. Anchors mark phase transitions. Context is assembled on demand — not accumulated, not compressed into lossy summaries.
- **Hooks all the way down.** The turn pipeline *is* hooks. Override `build_prompt`, `run_model`, or `render_outbound` to change behavior. The core does not privilege its own builtins.
- **One pipeline across channels.** CLI and Telegram share the same `process_inbound()` path. Hooks don't know which channel they're in.
- **Skills as documents.** Skills are `SKILL.md` files with validated frontmatter, not code modules with magic registration.

## Extend It

```python
from bub import hookimpl

class EchoPlugin:
    @hookimpl
    def build_prompt(self, message, session_id, state):
        return f"[echo] {message['content']}"

    @hookimpl
    async def run_model(self, prompt, session_id, state):
        return prompt
```

```toml
[project.entry-points."bub"]
echo = "my_package.plugin:EchoPlugin"
```

See the [Extension Guide](https://bub.build/extension-guide/) for hook semantics and plugin packaging.

## CLI

| Command | Description |
|---------|-------------|
| `bub chat` | Interactive REPL |
| `bub run MESSAGE` | One-shot turn |
| `bub gateway` | Channel listener (Telegram, etc.) |
| `bub login openai` | OpenAI Codex OAuth |
| `bub hooks` | Print hook-to-plugin bindings |

Lines starting with `,` enter internal command mode (`,help`, `,tools`, `,fs.read path=README.md`).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BUB_MODEL` | `openrouter:qwen/qwen3-coder-next` | Model identifier |
| `BUB_API_KEY` | — | Provider key (optional with `bub login openai`) |
| `BUB_API_BASE` | — | Custom provider endpoint |
| `BUB_API_FORMAT` | `completion` | `completion`, `responses`, or `messages` |
| `BUB_RUNTIME_MAX_STEPS` | `50` | Max tool-use loop iterations |
| `BUB_RUNTIME_MAX_TOKENS` | `1024` | Max tokens per model call |
| `BUB_RUNTIME_MODEL_TIMEOUT_SECONDS` | — | Model call timeout (seconds) |

## Background

We care less about whether an agent can finish a demo task, and more about whether it can coexist with real people under real conditions. Context is not baggage to carry forever — it is a working set, constructed when needed and let go when done.

Read more: [Context from Tape](https://tape.systems) · [Socialized Evaluation and Agent Partnership](https://bub.build/posts/2026-03-01-bub-socialized-evaluation-and-agent-partnership/)

## Docs

- [Architecture](https://bub.build/architecture/) — lifecycle, hook precedence, error handling
- [Features](https://bub.build/features/) — what ships today and current boundaries
- [CLI](https://bub.build/cli/) — commands and comma mode
- [Skills](https://bub.build/skills/) — discovery and authoring
- [Extension Guide](https://bub.build/extension-guide/) — hooks, tools, plugin packaging
- [Channels](https://bub.build/channels/) — adapters and sessions
- [Deployment](https://bub.build/deployment/) — Docker, Telegram, operations

## Development

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
```

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

[Apache-2.0](./LICENSE)
