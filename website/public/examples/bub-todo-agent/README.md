# Bub Todo Agent

A runnable end-to-end Bub SDK example: FastAPI + agent tools + Tape + agent events + SSE, with a frontend built on self-hosted UnoCSS and Alpine.js.

## Run

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
export BUB_MODEL=openai:gpt-4o-mini
export BUB_API_BASE=https://api.openai.com/v1
export BUB_API_KEY=sk-...
uv run main.py
```

You can also run `./main.py` directly (after `chmod +x main.py`). Dependencies are declared in the PEP 723 header at the top of `main.py`, so uv prepares the environment automatically — no `pyproject.toml` and no manual installs.

If you would rather not export variables every time, copy the example file and fill it in. It is loaded at startup, and existing environment variables take precedence over `.env`:

```bash
cp .env.example .env
# then edit .env: BUB_MODEL / BUB_API_BASE / BUB_API_KEY ...
```

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `BUB_MODEL` | `openai:gpt-4o-mini` | Model identifier passed to `republic.get_model()` |
| `BUB_API_BASE` | `https://api.openai.com/v1` | API base URL for OpenAI or a compatible service, mapped to `api_base` |
| `BUB_API_KEY` | none | API key; falls back to `REPUBLIC_OPENAI_API_KEY` when unset |
| `BUB_HOST` | `127.0.0.1` | Server bind address |
| `BUB_PORT` | `8000` | Server port |

These defaults live at the top of `main.py` and can be edited in place; environment variables and `.env` override them. When switching providers, adjust `MODEL`, `BASE_URL`, and `API_KEY` together. The composer reads `GET /config` to show the model, base URL, and key status — the endpoint never returns the key itself.

`Think level` under the composer can be `Auto`, `Low`, `Medium`, or `High`. `Auto` sends no reasoning options; the others pass `reasoning_effort`, and whether that is honored depends on the model and provider. The button in the top-right corner collapses or expands the todo panel.

Open <http://localhost:8000>.

## What to try

1. Click “Add a reading todo” and watch the tool call and the todo panel update.
2. Click the chip that marks “Write the weekly report” as complete to verify multi-turn memory.
3. Ask the agent to delete a todo: the run pauses and a prompt above the composer asks you to confirm. Cancel keeps the todo and tells the agent to suggest completing it; Delete lets `delete_task` run.
4. Refresh the page and ask “What todos did I add earlier?” to verify `FileTapeStore` persistence. The todo list resets on refresh, but the agent still remembers the conversation.

The confirmation is a `ToolCallStarted` hook. Bub awaits async handlers, so the hook can hold the run until the page answers `POST /chat/confirm`; an unanswered prompt fails closed after two minutes and the deletion is denied.

Todos live in memory only; every page load calls `POST /tasks/reset` to restore the seed data. Conversations are stored under `sessions/`.

## API

- `GET /` — chat page
- `GET /config` — model, base URL, and key status
- `GET /tasks` — real todo state, without calling the model
- `POST /tasks/reset` — restore the seed todos (called on page load)
- `PATCH /tasks/{task_id}` — change a todo's completion state
- `POST /chat/stream` — text, tool status, and confirmation requests over SSE
- `POST /chat/confirm` — approve or cancel the pending `delete_task` call

## Frontend dependencies

- UnoCSS runtime 66.12.0: `static/uno.global.js`
- Alpine.js 3.17.5: `static/alpine.min.js`

The page never loads anything from a third-party CDN. Third-party licenses are listed in `static/THIRD_PARTY_NOTICES.md`.

This is a single-process development example. It does not include the authentication, database, multi-instance locking, or session-lock reclamation a production deployment would need.
