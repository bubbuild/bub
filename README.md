# Bub

<div align="center">

<picture>
  <source srcset="https://raw.githubusercontent.com/bubbuild/bub/refs/heads/main/website/src/assets/bub-logo-dark.png" media="(prefers-color-scheme: dark)">
  <img alt="Bub logo" src="https://raw.githubusercontent.com/bubbuild/bub/refs/heads/main/website/src/assets/bub-logo.png" width="200">
</picture>

<p><strong>A tiny agent runtime, composable with plugins.</strong></p>

[Website](https://bub.build) · [Docs](https://bub.build/docs/getting-started/) · [Hub](https://hub.bub.build)

</div>

Bub is a small Python runtime for building agents in shared environments. It started in group chats, where multiple humans and agents had to work in the same conversation without hidden state, hand-wavy memory, or framework-specific magic.

Built on [agents.md](https://agents.md/) and [Agent Skills](https://agentskills.io/), Bub stays intentionally small. Every turn stage is a [pluggy](https://pluggy.readthedocs.io/) hook, builtins are replaceable, and the same runtime drives CLI, Telegram, and any channel you add.

## Quick Start

macOS and Linux:

```bash
curl -fsSL https://bub.build/install.sh | bash
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://bub.build/install.ps1 | iex"
```

Then:

```bash
bub chat                         # interactive session
bub run "summarize this repo"    # one-shot task
bub gateway                      # channel listener mode
```

See [Getting Started](https://bub.build/docs/getting-started/) for installer presets and source installs, and [Configure](https://bub.build/docs/operate/configure/) for model and runtime settings.

## Why Bub

- **Composable by design.** Every turn stage is a plugin hook. Override one stage or replace the whole flow without forking the runtime.
- **Tape context.** Context is rebuilt from append-only records, not carried around as mutable session state. Easier to inspect, replay, and hand off.
- **One runtime across surfaces.** The same inbound pipeline runs across CLI, Telegram, and custom channels. Adapters change the surface, not the runtime model.
- **Batteries included.** CLI, Telegram, tools, skills, and model execution ship with the core runtime. Use the defaults first, replace them later.
- **Operator equivalence.** Humans and agents work inside the same runtime boundaries, with the same evidence trail and handoff model.
- **Embeddable Python SDK.** `bub.core` exposes the agent loop on its own — bring your own model, tools, prompts, and tape store, with no settings or plugins required.

## How It Works

Every inbound message goes through one turn pipeline. Each stage is a hook.

```
resolve_session → load_state → build_prompt → run_model
                                                   ↓
              dispatch_outbound ← render_outbound ← save_state
```

Builtins are registered first and external plugins load after them, so later plugins take precedence. There are no framework-only shortcuts.

## Docs

- [Getting Started](https://bub.build/docs/getting-started/) — install Bub and run the first turn
- [Concepts](https://bub.build/docs/concepts/) — the mental model behind the runtime
- [Python SDK](https://bub.build/docs/build/sdk/) — embed Bub's agent loop in your own application
- [Build](https://bub.build/docs/build/) — write plugins, override hooks, ship tools and skills
- [Bub Hub](https://hub.bub.build) — browse community plugins, skills, and distributions
- [Channels](https://bub.build/docs/operate/channels/) — run Bub in CLI, Telegram, or your own channel
- [Deployment](https://bub.build/docs/operate/deploy/) — Docker, environment, upgrades

## Background

- [Why We Rewrote Bub](https://bub.build/posts/why-rewrite-bub/)
- [Socialized Evaluation and Agent Partnership](https://bub.build/posts/socialized-evaluation/)
- [Context from Tape](https://tape.systems)

## License

[Apache-2.0](https://github.com/bubbuild/bub/blob/main/LICENSE)
