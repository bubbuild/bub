# Bub

<div align="center">

<picture>
  <source srcset="https://raw.githubusercontent.com/bubbuild/bub/refs/heads/main/website/src/assets/bub-logo-dark.png" media="(prefers-color-scheme: dark)">
  <img alt="Bub logo" src="https://raw.githubusercontent.com/bubbuild/bub/refs/heads/main/website/src/assets/bub-logo.png" width="200">
</picture>

<p><strong>Build your Bub.</strong></p>

[Website](https://bub.build) · [Docs](https://bub.build/docs/) · [Hub](https://hub.bub.build)

</div>

Put an agent in your own Python program, or customize the Bub application. The application uses the same agent kernel and adds configuration, plugins, and channel integration.

## Build your own agent

Use `bub.core.Agent` to choose your model, tools, instructions, and conversation storage. The base `bub` package installs the kernel's dependencies.

In a Python 3.12+ project:

```bash
uv add bub
```

Run your first agent with the [SDK guide](https://bub.build/docs/sdk/), then [serve it with FastAPI](https://bub.build/docs/sdk/web-service/) when you need an HTTP endpoint.

You can also embed `BubAgent` to reuse the application's settings and plugin hooks in your service. This uses `bub[app]`; [Reuse application plugins](https://bub.build/docs/sdk/reuse-plugins/) explains the integration choices and their requirements.

## Customize the Bub app

Start a conversation, then add tools, skills, plugins, or channels for the work you want Bub to do.

Install on macOS or Linux:

```bash
curl -fsSL https://bub.build/install.sh | bash
```

On Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://bub.build/install.ps1 | iex"
```

Follow the prompts to choose plugins and configure your model, then start a conversation:

```bash
bub chat
```

[Run your first conversation](https://bub.build/docs/app/workspace/) walks through a task in your workspace. [Customize the Bub app](https://bub.build/docs/app/workspace/) covers tools, plugins, and packaging. For `uv tool install` and other setup options, see [Install Bub](https://bub.build/docs/getting-started/install/#app-install).

## Documentation

- [Choose a starting point](https://bub.build/docs/)
- [Shared concepts and capabilities](https://bub.build/docs/concepts/)
- [API and application reference](https://bub.build/docs/reference/)

## Background

- [Why We Rewrote Bub](https://bub.build/posts/why-rewrite-bub/)
- [Socialized Evaluation and Agent Partnership](https://bub.build/posts/socialized-evaluation/)
- [Context from Tape](https://tape.systems)

## License

[Apache-2.0](https://github.com/bubbuild/bub/blob/main/LICENSE)
