# Bub

**A common shape for agents that live alongside people.**

Bub started in group chats. Not as a demo or a personal assistant, but as a teammate that had to coexist with real humans and other agents in the same messy conversations — concurrent tasks, incomplete context, and nobody waiting.

It is hook-first, built on [pluggy](https://pluggy.readthedocs.io/), with a small core (~200 lines) and builtins that are just default plugins you can replace. Context comes from [tape](https://tape.systems) via [Republic](https://github.com/bubbuild/republic), not session accumulation. The same pipeline runs across CLI, Telegram, and any channel you add.

## Quick Start

```bash
pip install bub
```

Or from source:

```bash
git clone https://github.com/bubbuild/bub.git
cd bub
uv sync
cp env.example .env
```

```bash
uv run bub chat                         # interactive session
uv run bub run "summarize this repo"    # one-shot task
uv run bub gateway                      # channel listener mode
```

## How It Works

Every inbound message goes through one turn pipeline. Each stage is a hook.

```text
resolve_session → load_state → build_prompt → run_model_stream
                                                          ↓
                     dispatch_outbound ← render_outbound ← save_state
```

`run_model` remains supported as a compatibility hook and is adapted into a single-chunk stream when `run_model_stream` is absent.

Builtins are plugins registered first. Later plugins override earlier ones. No special cases.

## Read Next

- [Architecture](architecture.md) — lifecycle, hook precedence, error handling
- [Features](features.md) — what ships today and current boundaries
- [Channels](channels/index.md) — CLI, Telegram, and custom adapters
- [Skills](skills.md) — discovery and authoring
- [Extension Guide](extension-guide.md) — hooks, tools, plugin packaging
- [Deployment](deployment.md) — Docker, environment, upgrades
- [Posts](posts/index.md) — design notes
