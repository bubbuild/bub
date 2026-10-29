#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "bub @ git+https://github.com/bubbuild/bub@main",
#   "fastapi>=0.135",
#   "python-dotenv",
#   "uvicorn",
# ]
# ///

import asyncio
import logging
import os
from collections import defaultdict
from collections.abc import AsyncIterable, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Literal
from uuid import uuid4

import republic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bub.core import Agent
from bub.core.events import ToolCallDecision, ToolCallFinished, ToolCallStarted
from bub.core.store import FileTapeStore
from bub.core.tools import tool


logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent

load_dotenv()

# ===== App config: override via environment variables or .env, or edit the defaults here =====
MODEL = os.getenv("BUB_MODEL", "openai:gpt-4.1")
BASE_URL = os.getenv("BUB_API_BASE", "https://api.openai.com/v1")
API_KEY = os.getenv("BUB_API_KEY") or os.getenv("REPUBLIC_OPENAI_API_KEY")
HOST = os.getenv("BUB_HOST", "127.0.0.1")
PORT = int(os.getenv("BUB_PORT", "8000"))
# ==========================================================

# Called from the ToolCallStarted hook; resolves to True only if the person approves in the UI.
ConfirmDelete = Callable[[dict[str, object]], Awaitable[bool]]


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="seconds")


def initial_tasks() -> dict[str, dict[str, object]]:
    return {
        "task-1": {
            "task_id": "task-1",
            "title": "Write the weekly report",
            "completed": False,
            "created_at": _ago(days=2),
            "completed_at": None,
        },
        "task-2": {
            "task_id": "task-2",
            "title": "Read the Bub docs",
            "completed": True,
            "created_at": _ago(days=3),
            "completed_at": _ago(hours=5),
        },
    }


# Demo task store (not a Tape). Each page load resets it via POST /tasks/reset.
TASKS: dict[str, dict[str, object]] = initial_tasks()
TASK_IDS = count(3)


def reset_tasks() -> None:
    global TASK_IDS
    TASKS.clear()
    TASKS.update(initial_tasks())
    TASK_IDS = count(3)


def all_tasks() -> list[dict[str, object]]:
    return [dict(task) for task in TASKS.values()]


@tool
def list_tasks() -> list[dict[str, object]]:
    """List every todo with its task ID, title, and completion state."""
    return all_tasks()


class AddTaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=100, description="Short, specific todo title")


@tool(name="add_task", model=AddTaskInput)
def add_task(param: AddTaskInput) -> dict[str, object]:
    """Add a new incomplete todo."""
    task_id = f"task-{next(TASK_IDS)}"
    task = {
        "task_id": task_id,
        "title": param.title,
        "completed": False,
        "created_at": _ago(),
        "completed_at": None,
    }
    TASKS[task_id] = task
    return dict(task)


@tool
def complete_task(task_id: str) -> dict[str, object]:
    """Mark a todo complete by task ID. Call list_tasks first if the ID is unknown."""
    if task := TASKS.get(task_id):
        task["completed"] = True
        task["completed_at"] = _ago()
        return dict(task)
    return {"task_id": task_id, "error": "task_not_found"}


@tool
def delete_task(task_id: str) -> dict[str, object]:
    """Permanently delete a todo by task ID. The host must authorize it first."""
    if task := TASKS.pop(task_id, None):
        return {"task_id": task_id, "status": "deleted", "title": task["title"]}
    return {"task_id": task_id, "error": "task_not_found"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with republic.get_model(MODEL, api_base=BASE_URL, api_key=API_KEY) as model:
        agent = Agent(
            store=FileTapeStore(BASE_DIR / "sessions"),
            tools=[list_tasks, add_task, complete_task, delete_task],
            system_prompt=(
                "You are a personal todo assistant. Always use tools to read or change todos; "
                "never guess task state. If a request is ambiguous, look it up or ask the user "
                "first, and respect tool errors and denial reasons."
            ),
            max_steps=6,
            max_tool_concurrency=2,
        )

        @agent.events.on(ToolCallStarted)
        async def authorize_tool(event: ToolCallStarted) -> None:
            # Async handlers are awaited, so this hook pauses the run until the person answers
            # the prompt. Denying is the safe default when nobody answers.
            if event.call.tool != "delete_task":
                return
            confirm: ConfirmDelete | None = event.state.get("confirm_delete")
            if confirm is None or not await confirm(event.call.arguments):
                event.decision = ToolCallDecision.deny(
                    "The user did not approve deleting this todo. Do not retry; offer to mark "
                    "the todo complete instead."
                )

        @agent.events.on(ToolCallFinished)
        def audit_tool(event: ToolCallFinished) -> None:
            logger.info(
                "tool=%s status=%s duration_ms=%s",
                event.result.tool,
                "error" if event.result.error else "ok",
                event.result.duration_ms,
            )

        app.state.model = model
        app.state.agent = agent
        app.state.session_locks = defaultdict(asyncio.Lock)
        # request_id -> future that the delete hook is waiting on; answered by POST /chat/confirm.
        app.state.pending_confirms = {}
        yield


app = FastAPI(title="Bub Todo Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ChatRequest(BaseModel):
    # The tape name becomes a filename; namespace it per tenant/user in production.
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    prompt: str = Field(min_length=1, max_length=10_000)
    think_level: Literal["auto", "low", "medium", "high"] = "auto"


def run_agent(agent: Agent, model, req: ChatRequest, confirm_delete: ConfirmDelete):
    # Auto sends no reasoning options; other levels depend on the current provider.
    options = None if req.think_level == "auto" else {"reasoning_effort": req.think_level}
    return agent.run(
        req.prompt,
        model=model,
        options=options,
        tape=req.session_id,
        # Turn state reaches event hooks and tools; the delete hook awaits this callback.
        state={"session_id": req.session_id, "confirm_delete": confirm_delete},
    )


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/config")
async def config():
    # Only expose display-safe config; never return the API key.
    return {"model": MODEL, "base_url": BASE_URL, "api_key_configured": bool(API_KEY)}


@app.get("/tasks")
async def tasks():
    # Deterministic business query; no model call.
    return {"tasks": all_tasks()}


@app.post("/tasks/reset")
async def reset_demo_tasks():
    # Called on every page load so a refresh starts from the seed data.
    reset_tasks()
    return {"tasks": all_tasks()}


class TaskUpdate(BaseModel):
    completed: bool


@app.patch("/tasks/{task_id}")
async def update_task(task_id: str, req: TaskUpdate):
    # Manual toggle from the page; shares the same TASKS store as the agent tools.
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    task["completed"] = req.completed
    task["completed_at"] = _ago() if req.completed else None
    return dict(task)


class Confirmation(BaseModel):
    request_id: str
    approved: bool


@app.post("/chat/confirm")
async def confirm_tool_call(req: Confirmation, request: Request):
    # Answer the delete confirmation a running hook is awaiting; unknown means already gone.
    future = request.app.state.pending_confirms.pop(req.request_id, None)
    if future is None or future.done():
        raise HTTPException(status_code=404, detail="confirmation_not_found")
    future.set_result(req.approved)
    return {"approved": req.approved}


@app.post("/chat/stream", response_class=EventSourceResponse)
async def chat_stream(req: ChatRequest, request: Request) -> AsyncIterable[ServerSentEvent]:
    state = request.app.state
    lock: asyncio.Lock = state.session_locks[req.session_id]
    # The run is pumped in a task so a hook can push frames (the confirmation request) because
    # it runs inside the agent stream, where the response generator itself cannot yield.
    queue: asyncio.Queue[ServerSentEvent | None] = asyncio.Queue()

    async def confirm_delete(arguments: dict[str, object]) -> bool:
        task_id = str(arguments.get("task_id", ""))
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        state.pending_confirms[request_id] = future
        queue.put_nowait(
            ServerSentEvent(
                data={
                    "request_id": request_id,
                    "task_id": task_id,
                    "title": (TASKS.get(task_id) or {}).get("title"),
                },
                event="confirm",
            )
        )
        try:
            # Fail closed: no answer within two minutes, or a dropped client, means no delete.
            return await asyncio.wait_for(future, timeout=120)
        except TimeoutError:
            return False
        finally:
            state.pending_confirms.pop(request_id, None)

    async def chat() -> None:
        try:
            async with lock, run_agent(state.agent, state.model, req, confirm_delete) as stream:
                async for event in stream:
                    if event.kind == "text":
                        queue.put_nowait(ServerSentEvent(data={"delta": event.data["delta"]}, event="message"))
                    elif event.kind == "tool_call":
                        names = [call["name"] for call in event.data["tool_calls"]]
                        queue.put_nowait(ServerSentEvent(data={"names": names}, event="tool"))

                # The run has ended; usage is for the last model step only.
                queue.put_nowait(ServerSentEvent(data={"last_step_usage": stream.usage}, event="done"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Streaming agent run failed: session=%s", req.session_id)
            queue.put_nowait(ServerSentEvent(data={"message": "Agent run failed"}, event="error"))
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(chat())
    try:
        while (frame := await queue.get()) is not None:
            yield frame
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
