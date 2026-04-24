# Bub

<div align="center">

<picture>
  <source srcset="https://raw.githubusercontent.com/bubbuild/bub/refs/heads/main/website/src/assets/bub-logo-dark.png" media="(prefers-color-scheme: dark)">
  <img alt="Bub logo" src="https://raw.githubusercontent.com/bubbuild/bub/refs/heads/main/website/src/assets/bub-logo.png" width="200">
</picture>

<p><strong>A hook-first runtime for agents that live alongside people.</strong></p>

</div>

Bub is a small Python runtime for building agents in shared environments. It started in group chats, where multiple humans and agents had to work in the same conversation without hidden state, hand-wavy memory, or framework-specific magic.

Built on [agents.md](https://agents.md/) and [Agent Skills](https://agentskills.io/) , Bub stays intentionally small. Every turn stage is a [pluggy](https://pluggy.readthedocs.io/) hook. Builtins are included but replaceable. The same runtime drives CLI, Telegram, and any channel you add.

[Website](https://bub.build) · [GitHub](https://github.com/bubbuild/bub)

## Quick Start

```bash
pip install bub
```

Or from source:

```bash
git clone https://github.com/bubbuild/bub.git
cd bub
uv sync  # enough to run Bub from source
```

For local development, use `make install` instead so the website toolchain and `prek` hooks are installed too.

```bash
uv run bub chat                         # interactive session
uv run bub run "summarize this repo"    # one-shot task
uv run bub gateway                      # channel listener mode
```

## Why Bub

- **Hook-first runtime.** Every turn stage is a hook. Override one stage or replace the whole flow without forking the runtime.
- **Tape context.** Context is rebuilt from append-only records, not carried around as mutable session state. Easier to inspect, replay, and hand off.
- **One runtime across surfaces.** The same inbound pipeline runs across CLI, Telegram, and custom channels. Adapters change the surface, not the runtime model.
- **Batteries included.** CLI, Telegram, tools, skills, and model execution ship with the core runtime. Use the defaults first, replace them later.
- **Operator equivalence.** Humans and agents work inside the same runtime boundaries, with the same evidence trail and handoff model. No hidden operator class.

## How It Works

Every inbound message goes through one turn pipeline. Each stage is a hook.

```
resolve_session → load_state → build_prompt → run_model
                                                   ↓
              dispatch_outbound ← render_outbound ← save_state
```

Builtins are registered first. External plugins load after them. At runtime, later plugins take precedence. There are no framework-only shortcuts.

Key source files:

- Turn orchestrator: [`src/bub/framework.py`](https://github.com/bubbuild/bub/blob/main/src/bub/framework.py)
- Hook contract: [`src/bub/hookspecs.py`](https://github.com/bubbuild/bub/blob/main/src/bub/hookspecs.py)
- Builtin hooks: [`src/bub/builtin/hook_impl.py`](https://github.com/bubbuild/bub/blob/main/src/bub/builtin/hook_impl.py)
- Skill discovery: [`src/bub/skills.py`](https://github.com/bubbuild/bub/blob/main/src/bub/skills.py)

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

See the [Extending docs](https://bub.build/docs/extending/) for hook guides, packaging, and plugin structure.

## CLI

| Command            | Description                       |
| ------------------ | --------------------------------- |
| `bub chat`         | Interactive REPL                  |
| `bub run MESSAGE`  | One-shot turn                     |
| `bub gateway`      | Channel listener (Telegram, etc.) |
| `bub install`      | Install or sync Bub plugin deps   |
| `bub update`       | Upgrade Bub plugin deps           |
| `bub login openai` | OpenAI Codex OAuth                |

Lines starting with `,` enter internal command mode (`,help`, `,skill name=my-skill`, `,fs.read path=README.md`).

`bub hooks` still exists for diagnostics, but it is hidden from top-level help. `bub install` and `bub update` manage a separate uv project for Bub plugins, defaulting to `~/.bub/bub-project` or `BUB_PROJECT`.

## Configuration

| Variable                    | Default                      | Description                                          |
| --------------------------- | ---------------------------- | ---------------------------------------------------- |
| `BUB_MODEL`                 | `openrouter:openrouter/free` | Model identifier                                     |
| `BUB_API_KEY`               | —                            | Provider key (optional with `bub login openai`)      |
| `BUB_API_BASE`              | —                            | Custom provider endpoint                             |
| `BUB_API_FORMAT`            | `completion`                 | `completion`, `responses`, or `messages`             |
| `BUB_CLIENT_ARGS`           | —                            | JSON object forwarded to the underlying model client |
| `BUB_MAX_STEPS`             | `50`                         | Max tool-use loop iterations                         |
| `BUB_MAX_TOKENS`            | `1024`                       | Max tokens per model call                            |
| `BUB_MODEL_TIMEOUT_SECONDS` | —                            | Model call timeout (seconds)                         |

## Background

Bub is shaped by one constraint: real collaboration is messier than a solo demo. In shared environments, operators need visible boundaries, auditable history, and extension points that do not collapse into framework sprawl.

Read more:

- [Why We Rewrote Bub](https://bub.build/posts/why-rewrite-bub/)
- [Socialized Evaluation and Agent Partnership](https://bub.build/posts/socialized-evaluation/)
- [Context from Tape](https://tape.systems)

## Docs

- [Getting Started](https://bub.build/docs/getting-started/) — install Bub and run the first turn
- [Architecture](https://bub.build/docs/concepts/architecture/) — the mental model behind the runtime
- [Channels](https://bub.build/docs/guides/channels/) — run Bub in CLI, Telegram, or your own channel
- [Skills](https://bub.build/docs/guides/skills/) — discover, inspect, and author Agent Skills in Bub
- [Extending](https://bub.build/docs/extending/) — write plugins, override hooks, ship tools and skills
- [Deployment](https://bub.build/docs/guides/deployment/) — Docker, environment, upgrades

## Development

```bash
make install
make check
make test
make docs
make docs-test
```

See [CONTRIBUTING.md](https://github.com/bubbuild/bub/blob/main/CONTRIBUTING.md).

## License

[Apache-2.0](https://github.com/bubbuild/bub/blob/main/LICENSE)
