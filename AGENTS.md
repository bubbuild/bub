# Repository Guidelines

## Project Structure & Module Organization

Core code lives under `src/` and `packages/`:

- `src/bub/__main__.py`: Typer CLI entrypoint.
- `packages/bubling/`: the agent kernel, published as the separate `bubling` distribution and released in lockstep with `bub`; it has no settings or plugin dependencies: agent loop over a tape store (`agent.py`), model protocol and Republic chat model adapter (`model.py`), agent events and their bus (`events.py`), the tape recorder handler (`recorder.py`), system prompt sections (`system_prompt.py`), command prompts (`commands.py`), tools, tape, store, and tape context, plus the kernel vocabulary it shares with the framework (`errors.py`, `prompt.py`, `streaming.py`, `tracing.py`, `turn.py`). Nothing in `bubling` imports `bub`. Its tests live in `packages/bubling/tests/` and must run with only `bubling` installed.
- `src/bub/{errors,tracing,streaming,store,tape,tools,sidecars}.py`: compatibility aliases of the `bubling` modules of the same name.
- `src/bub/framework.py`: turn orchestration and outbound routing.
- `src/bub/hooks/`: hook specifications, execution helpers, and interception contracts.
- `src/bub/channels/`: channel bases, routing contracts, admission policy, and lifecycle manager.
- `src/bub/{envelope,turn,model_selection}.py`: small framework vocabulary owned by each concern.
- `src/bub/builtin/`: builtin runtime: `BubAgent` (a `bubling.Agent` subclass adding settings, skills, commands, steering), `BubModel`, tape archiving, CLI wiring, tools, tape services, and the fuzzy-searching file tape store.
- `src/bub/channels/`: channel abstractions plus CLI and Telegram adapters.
- `src/bub/skills.py` / `src/bub/tools.py`: skill discovery and tool registry.
- `src/skills/`: bundled skills shipped with Bub.

Tests live in `tests/`. The website and docs runtime lives in `website/`.

## Build, Test, and Development Commands

- `uv sync`: install or update the Python environment; enough to run Bub from source or for deployment-only hosts.
- `make install`: full local development bootstrap; sync Python deps, install website deps, and install `prek` hooks.
- `uv run bub chat`: run the interactive CLI.
- `uv run bub gateway`: start channel listener mode.
- `uv run bub run "hello"`: run one inbound message through the full framework pipeline.
- `uv run bub hooks`: inspect discovered hook bindings.
- `uv run ruff check .`: run Ruff directly when you only want lint feedback.
- `uv run mypy`: run mypy against `src/` and `packages/bubling/src/`.
- `uv run prek run import-linter -a`: check the import contracts in `pyproject.toml` (`bubling` must not import `bub` or application dependencies); `prek` and `make check` run it too.
- `uv run pytest -q`: run the main test suite without doctests.
- `make test`: run pytest with doctests enabled.
- `uv run --isolated --no-project --with ./packages/bubling --with pytest --with pytest-asyncio pytest packages/bubling/tests`: run `bubling`'s tests with only `bubling` installed, as CI does.
- `make build`: build the distributions of both `bub` and `bubling`.
- Releases: publishing a GitHub release builds `bub` and `bubling` at the tag's version, with `bub` pinned to `bubling==<tag>` by `scripts/release.py` (the repository keeps a range for development). Rehearse with `python scripts/release.py pin X`, `SETUPTOOLS_SCM_PRETEND_VERSION=X uv build --all-packages`, `python scripts/release.py check X dist`, then revert `pyproject.toml`.
- `make check`: lock validation, `prek` (including import contracts), and typing.
- `make docs` / `make docs-test` / `make docs-preview`: serve, build, or preview the Astro website/docs in `website/`.

## Coding Style & Naming Conventions

- Python 3.12+, 4-space indentation, and type hints for new or modified logic.
- Use `snake_case` for modules/functions/variables, `PascalCase` for classes, and `UPPER_CASE` for constants.
- Keep functions focused and composable; avoid hidden side effects.
- Format and lint with Ruff. Keep line length within 120 unless an existing file clearly follows a different local convention.

## Testing Guidelines
- Framework: `pytest`.
- Name test files `tests/test_<feature>.py`.
- Tests that need only the kernel go in `packages/bubling/tests/` and must not import `bub` or `tests`.
- Prefer behavior-oriented test names such as `test_gateway_uses_enabled_channels_only`.
- Cover hook precedence, turn lifecycle, CLI/channel behavior, and tape persistence when changing runtime behavior.
- Update or add tests in the same change when behavior moves.

## Commit & Pull Request Guidelines

- Follow the Conventional Commit style used in history, for example `feat:`, `fix:`, `docs:`, `chore:`.
- Keep commits focused; avoid mixing unrelated refactors with behavior changes.
- For PRs, include:
  - what changed and why
  - impacted modules or commands
  - verification performed (`ruff`, `mypy`, `pytest`, docs build if relevant)
  - docs updates when CLI behavior, commands, or architecture changed

## Security & Configuration Tips

- Use `.env` for local secrets; never commit credentials.
- Bub runtime settings are driven by `BUB_*` variables such as `BUB_MODEL`, `BUB_API_KEY`, and `BUB_API_BASE`.
- Provider-specific keys such as `OPENROUTER_API_KEY` may still be consumed by downstream SDKs.
- Telegram deployments usually require `BUB_TELEGRAM_TOKEN`, and allowlists are controlled with `BUB_TELEGRAM_ALLOW_USERS` and `BUB_TELEGRAM_ALLOW_CHATS`.
