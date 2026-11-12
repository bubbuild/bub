"""Subagents: child runs of a Bub agent, run as background tasks.

The ``subagent`` tool starts a child run of the same agent in the background, tracks it
as a :class:`SubagentTask` and returns its task ID at once. The result reaches the parent
as a ``<subagent-notification>`` message, unless the parent read it first with
``subagent.wait``: a run that started tasks waits for them when a step would end the run
(see :func:`deliver_subagent_results`), and a task that ends after its run reaches the
session through the steering inbox.

Each task publishes :class:`SubagentStarted`, :class:`SubagentOutput` (one per child
stream event) and :class:`SubagentFinished` on the agent's event bus, so channels can
show child runs without replacing the tool.

Subagent profiles are markdown files with YAML front matter in ``.agents/agents/``
(workspace, then home): the front matter sets ``description``, and optionally
``model``, ``tools``, ``skills``, ``reasoning_effort`` and ``timeout_seconds``; the body
is added to the child's system prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Collection, Iterable
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import republic
import yaml
from loguru import logger
from pydantic import BaseModel, Field

from bub.core.events import AgentEvent, EventBus, MessagesPending
from bub.core.tools import ToolContext, tool
from bub.streaming import StreamEvent
from bub.turn import TurnState

if TYPE_CHECKING:
    from bub.builtin.agent import BubAgent
    from bub.core.tape import Tape

SubagentStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
TERMINAL_STATUSES: tuple[SubagentStatus, ...] = ("completed", "failed", "cancelled")

SUBAGENT_TOOL_NAME = "subagent"
SUBAGENT_THREAD_PREFIX = "subagent:"
"""Prefix of a child run's ``_runtime_thread_id``, which keys its steering messages."""
SUBAGENT_CHANNEL = "subagent"
SUBAGENT_INSTRUCTIONS_STATE_KEY = "_runtime_subagent_instructions"
"""A child run's profile instructions, added to its system prompt."""
PROFILES_DIR = ".agents/agents"
DEFAULT_WAIT_TIMEOUT_SECONDS = 60
MAX_RETAINED_TASKS = 200

_PARENT_ONLY_STATE_KEYS = frozenset({
    # Keys the steering inbox, the parent's tool scope and its running tape; a child gets its own.
    "_runtime_thread_id",
    "_runtime_allowed_tools",
    "_runtime_tool_names",
    "_runtime_code_tools",
    "_runtime_tape",
    "allowed_skills",
    SUBAGENT_INSTRUCTIONS_STATE_KEY,
})


class SubAgentInput(BaseModel):
    prompt: str | list[dict] = Field(
        ..., description="The task for the sub-agent, either as a string or a list of message parts."
    )
    agent: str | None = Field(
        None, description="Name of a subagent profile to use; its settings apply unless given here."
    )
    description: str | None = Field(None, description="A short label for the task, shown in task lists.")
    model: str | None = Field(None, description="The model to use for the sub-agent.")
    reasoning_effort: str | None = Field(None, description="The reasoning effort of the sub-agent's model.")
    session: str = Field(
        "temp",
        description=(
            "'temp' (default): a new empty conversation that is discarded afterwards. "
            "'fork': starts from a copy of the current conversation, discarded afterwards. "
            "Any other value names a session that is kept and can be continued later."
        ),
    )
    allowed_tools: list[str] | None = Field(
        None,
        description="Tool names the sub-agent may use; defaults to the tools available to the current agent.",
    )
    allowed_skills: list[str] | None = Field(
        None,
        description="Skill names the sub-agent may use; defaults to the skills available to the current agent.",
    )
    timeout_seconds: int | None = Field(None, gt=0, description="Stop the sub-agent after this many seconds.")


class SubAgentResult(TypedDict):
    task_id: str
    session_id: str
    status: SubagentStatus
    output: str
    errors: list[str]


@dataclass(frozen=True)
class SubagentProfile:
    """A named subagent configuration loaded from a markdown file."""

    name: str
    description: str
    instructions: str
    location: Path
    model: str | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int | None = None


@dataclass
class _RunRequest:
    """How to run a task's child: everything but its prompt, kept to continue the session."""

    agent: BubAgent
    session: str
    fork_from: Tape | None
    model: str | None
    reasoning_effort: str | None
    allowed_tools: set[str]
    allowed_skills: list[str] | None
    instructions: str | None
    timeout_seconds: int | None
    base_state: TurnState


@dataclass(eq=False)
class SubagentTask:
    """One subagent run and its outcome.

    ``persistent`` tasks run on a named session that outlives them, so ``subagent.send``
    can continue it after the task ends.
    """

    task_id: str
    session_id: str
    parent_session_id: str
    description: str
    model: str | None
    profile: str | None
    persistent: bool
    parent_call_id: str | None = None
    """The ID of the tool call that started the task, when the model made it."""
    status: SubagentStatus = "queued"
    output: str = ""
    errors: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    steps: int = 0
    tool_calls: int = 0
    last_tool: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    delivered: bool = False
    """Whether the result was handed to the parent; a delivered result sends no further notification."""
    _parent_key: TurnState = field(default_factory=dict, repr=False)
    _request: _RunRequest | None = field(default=None, repr=False)
    _events: EventBus | None = field(default=None, repr=False)
    _parent_state: TurnState = field(default_factory=dict, repr=False)
    _runner: asyncio.Task[None] | None = field(default=None, repr=False)
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _waiters: int = field(default=0, repr=False)
    _pending_text: list[str] = field(default_factory=list, repr=False)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def thread_id(self) -> str:
        """The child run's ``_runtime_thread_id``, keying the messages ``subagent.send`` steers into it."""
        return f"{SUBAGENT_THREAD_PREFIX}{self.task_id}"

    def result(self) -> SubAgentResult:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status,
            "output": self.output,
            "errors": list(self.errors),
        }

    def observe(self, event: StreamEvent) -> None:
        """Track the child stream: the answer is the text of the last step that made no tool calls."""
        if event.kind == "text":
            self._pending_text.append(str(event.data.get("delta", "")))
        elif event.kind == "tool_call":
            calls = event.data.get("tool_calls") or []
            self.tool_calls += len(calls)
            if calls and isinstance(calls[-1], dict):
                self.last_tool = str(calls[-1].get("name") or (calls[-1].get("function") or {}).get("name") or "")
        elif event.kind == "error":
            self.errors.append(str(event.data.get("message") or event.data.get("error") or "unknown error"))
        elif event.kind == "final":
            self.steps += 1
            if not (event.data.get("tool_calls") or event.data.get("tool_results")):
                self.output = "".join(self._pending_text)
            self._pending_text.clear()

    def finish_output(self) -> None:
        """Fall back to the text of an unfinished last step when no step answered."""
        if not self.output and self._pending_text:
            self.output = "".join(self._pending_text)
        self._pending_text.clear()

    async def wait(self) -> None:
        await self._done.wait()


@dataclass(kw_only=True)
class SubagentEvent(AgentEvent):
    """A subagent task changed; ``state`` is the turn state of the run that started it."""

    task: SubagentTask


@dataclass(kw_only=True)
class SubagentStarted(SubagentEvent):
    """A subagent task left the queue and its child run is starting."""


@dataclass(kw_only=True)
class SubagentOutput(SubagentEvent):
    """The child run produced ``event``; ``task`` already reflects it."""

    event: StreamEvent


@dataclass(kw_only=True)
class SubagentFinished(SubagentEvent):
    """A subagent task ended as ``completed``, ``failed`` or ``cancelled``."""


class SubagentManager:
    """Own subagent tasks across turns, limit how many run at once, and stop them on exit."""

    def __init__(self) -> None:
        self._tasks: dict[str, SubagentTask] = {}
        self._slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Cancel the subagent tasks still running when this runtime exits."""
        try:
            yield
        finally:
            await self._cancel_tasks([task for task in self._tasks.values() if not task.done])

    def get(self, task_id: str) -> SubagentTask:
        try:
            return self._tasks[task_id.strip()]
        except KeyError:
            raise ValueError(f"unknown subagent task: {task_id}") from None

    def tasks(self, parent_session_id: str | None = None) -> list[SubagentTask]:
        """Return tasks, oldest first, optionally only those started by one session."""
        return [
            task
            for task in self._tasks.values()
            if parent_session_id is None or task.parent_session_id == parent_session_id
        ]

    async def start(
        self,
        request: _RunRequest,
        prompt: str | list[dict],
        *,
        events: EventBus | None,
        parent_state: TurnState,
        description: str | None,
        profile: str | None,
        parent_call_id: str | None = None,
    ) -> SubagentTask:
        """Queue a child run and return its task; the run starts once a slot is free."""
        task_id = f"sub-{uuid.uuid4().hex[:8]}"
        persistent = request.session not in ("temp", "fork")
        parent_session_id = str(parent_state.get("session_id") or "")
        task = SubagentTask(
            task_id=task_id,
            session_id=request.session if persistent else f"temp/{task_id}",
            parent_session_id=parent_session_id,
            description=description or _describe(prompt),
            model=request.model,
            profile=profile,
            persistent=persistent,
            parent_call_id=parent_call_id,
            _parent_key=_steering_key(parent_state),
            _request=request,
            _events=events,
            _parent_state=parent_state,
        )
        self._prune()
        self._tasks[task_id] = task
        task._runner = asyncio.create_task(self._run(task, prompt), name=f"subagent {task_id}")
        return task

    async def wait(self, tasks: Iterable[SubagentTask], timeout: float | None, *, any_task: bool = False) -> None:
        """Wait until every task (or, with ``any_task``, one) ends or ``timeout`` seconds pass.

        Ended tasks count as delivered: their results go to the waiter, not to a notification.
        """
        tasks = list(tasks)
        if not tasks:
            return
        for task in tasks:
            task._waiters += 1
        waiters = [asyncio.ensure_future(task.wait()) for task in tasks]
        try:
            await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED if any_task else asyncio.ALL_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
            for task in tasks:
                task._waiters -= 1
                if task.done:
                    task.delivered = True

    def unread(self, state: TurnState) -> list[SubagentTask]:
        """Tasks started by the run whose turn state is ``state`` and not delivered yet."""
        return [task for task in self._tasks.values() if task._parent_state is state and not task.delivered]

    async def cancel(self, task_id: str) -> SubagentTask:
        task = self.get(task_id)
        await self._cancel_tasks([task])
        return task

    async def cancel_session(self, parent_session_id: str) -> int:
        """Cancel the running tasks a session started; return how many."""
        tasks = [task for task in self.tasks(parent_session_id) if not task.done]
        await self._cancel_tasks(tasks)
        return len(tasks)

    async def steer(self, task: SubagentTask, message: str) -> bool:
        """Queue ``message`` for a running task's next step; False if it cannot be delivered."""
        if task.done or task._request is None:
            return False
        framework = getattr(task._request.agent, "framework", None)
        if framework is None:
            return False
        return bool(
            await framework.steer_message(
                message=_channel_message(task.session_id, task.task_id, message),
                session_id=task.session_id,
                state={"session_id": task.session_id, "_runtime_thread_id": task.thread_id},
            )
        )

    async def _cancel_tasks(self, tasks: list[SubagentTask]) -> None:
        runners = [task._runner for task in tasks if task._runner is not None and not task._runner.done()]
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        for task in tasks:
            if not task.done:
                # Cancelled before its runner started: the run body, and its cleanup, never ran.
                task.status = "cancelled"
                task.ended_at = time.time()
                await self._settle(task)

    def _prune(self) -> None:
        """Forget the oldest read results beyond ``MAX_RETAINED_TASKS``."""
        finished = [task for task in self._tasks.values() if task.done and task.delivered]
        for task in finished[: max(0, len(self._tasks) - MAX_RETAINED_TASKS + 1)]:
            self._tasks.pop(task.task_id, None)

    @contextlib.asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        from bub.builtin.settings import load_settings

        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots_loop is not loop:
            self._slots = asyncio.Semaphore(load_settings().subagent_max_concurrency)
            self._slots_loop = loop
        async with self._slots:
            yield

    async def _run(self, task: SubagentTask, prompt: str | list[dict]) -> None:
        request = cast("_RunRequest", task._request)
        events, parent_state = task._events, task._parent_state
        try:
            async with self._slot():
                task.status = "running"
                task.started_at = time.time()
                await _publish(events, SubagentStarted(state=parent_state, task=task))
                async with asyncio.timeout(request.timeout_seconds):
                    stream = await request.agent.run_stream(
                        session_id=task.session_id,
                        prompt=prompt,
                        state=_child_state(request, task),
                        model=request.model,
                        allowed_tools=request.allowed_tools,
                        allowed_skills=request.allowed_skills,
                        reasoning_effort=request.reasoning_effort,
                        fork_from=request.fork_from,
                    )
                    async with aclosing(stream):
                        async for event in stream:
                            task.observe(event)
                            await _publish(events, SubagentOutput(state=parent_state, task=task, event=event))
                    task.usage = getattr(stream, "usage", None)
            task.finish_output()
            task.status = "failed" if task.errors and not task.output else "completed"
        except asyncio.CancelledError:
            task.finish_output()
            task.status = "cancelled"
            raise
        except TimeoutError:
            task.finish_output()
            task.status = "failed"
            task.errors.append(f"timed out after {request.timeout_seconds} seconds")
        except Exception as exc:
            logger.opt(exception=exc).warning("subagent.failed task_id={}", task.task_id)
            task.finish_output()
            task.status = "failed"
            task.errors.append(f"{exc!s}" or type(exc).__name__)
        finally:
            task.ended_at = time.time()
            await self._settle(task)

    async def _settle(self, task: SubagentTask) -> None:
        """Clean up a finished task, publish its end and notify the parent of an unread result."""
        request = cast("_RunRequest", task._request)
        events, parent_state = task._events, task._parent_state
        framework = getattr(request.agent, "framework", None)
        inbox = framework.get_steering_inbox() if framework is not None else None
        child_key = {"session_id": task.session_id, "_runtime_thread_id": task.thread_id}
        if inbox is not None and (undelivered := await inbox.drain_messages(child_key)):
            task.errors.append(f"{len(undelivered)} message(s) sent to the task arrived after it finished")
        if not task.persistent:
            from bub.builtin.shell_manager import shell_manager

            # Nobody can reach a temporary session's background shells once its run ends.
            await shell_manager.terminate_session(task.session_id)
        task._done.set()
        await _publish(events, SubagentFinished(state=parent_state, task=task))
        if not task.delivered and task._waiters == 0 and framework is not None:
            # The parent session reads it with its next step, or with its next turn.
            task.delivered = True
            await framework.steer_message(
                message=_channel_message(task.parent_session_id, task.task_id, render_notification(task)),
                session_id=task.parent_session_id,
                state=dict(task._parent_key),
            )


subagent_manager = SubagentManager()


async def _publish(events: EventBus | None, event: SubagentEvent) -> None:
    """Deliver an observation event; a failing handler must not fail the child run."""
    if events is None:
        return
    try:
        await events.emit(event)
    except Exception:
        logger.exception("subagent.event_handler_failed event={}", type(event).__name__)


def _steering_key(state: TurnState) -> TurnState:
    key: TurnState = {"session_id": state.get("session_id")}
    if thread_id := state.get("_runtime_thread_id"):
        key["_runtime_thread_id"] = thread_id
    return key


def _child_state(request: _RunRequest, task: SubagentTask) -> TurnState:
    state = {key: value for key, value in request.base_state.items() if key not in _PARENT_ONLY_STATE_KEYS}
    state["session_id"] = task.session_id
    state["_runtime_thread_id"] = task.thread_id
    if request.instructions:
        state[SUBAGENT_INSTRUCTIONS_STATE_KEY] = request.instructions
    return state


def _channel_message(session_id: str, task_id: str, content: str) -> Any:
    from bub.channels.message import ChannelMessage

    return ChannelMessage(session_id=session_id, channel=SUBAGENT_CHANNEL, chat_id=task_id, content=content)


def _describe(prompt: str | list[dict]) -> str:
    if isinstance(prompt, str):
        text = prompt
    else:
        text = next((str(part.get("text", "")) for part in prompt if part.get("type") == "text"), "")
    first_line = next((line.strip() for line in text.strip().splitlines() if line.strip()), "")
    return first_line[:120]


def render_notification(task: SubagentTask) -> str:
    """The message telling the parent a background task ended."""
    body = task.output.strip() or "(no output)"
    errors = "".join(f"\n[Error: {message}]" for message in task.errors)
    return (
        f'<subagent-notification task_id="{task.task_id}" status="{task.status}">\n'
        f"{body}{errors}\n</subagent-notification>"
    )


def _render_result(result: SubAgentResult | str) -> str:
    if isinstance(result, str):
        return result
    errors = "".join(f"[Error: {message}]" for message in result["errors"])
    if result["status"] in ("queued", "running"):
        return (
            f"Subagent task {result['task_id']} is running in the background (session {result['session_id']}). "
            "Its result arrives as a <subagent-notification> message before this turn ends, so keep working "
            "or finish your reply; use subagent.wait to get it sooner, subagent.send to message the task, "
            "or subagent.cancel to stop it."
        )
    if result["status"] == "cancelled":
        partial = f"\nPartial output:\n{result['output']}" if result["output"] else ""
        return f"Subagent task {result['task_id']} was cancelled.{partial}"
    return result["output"] + errors


def _render_task_list(results: list[SubAgentResult]) -> str:
    if not results:
        return "(no subagent tasks)"
    return "\n\n".join(
        f"[{result['task_id']}] status: {result['status']} session: {result['session_id']}"
        + (f"\n{_render_result(result)}" if result["status"] in TERMINAL_STATUSES else "")
        for result in results
    )


# ---------------------------------------------------------------- profiles


def discover_subagent_profiles(workspace: Path) -> dict[str, SubagentProfile]:
    """Load subagent profiles from the workspace's, then the home directory's ``.agents/agents``.

    A workspace profile overrides a home profile of the same (case-insensitive) name.
    """
    profiles: dict[str, SubagentProfile] = {}
    for root in (workspace / PROFILES_DIR, Path.home() / PROFILES_DIR):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            profile = _read_profile(path)
            if profile is not None:
                profiles.setdefault(profile.name.casefold(), profile)
    return profiles


def _read_profile(path: Path) -> SubagentProfile | None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        return None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        logger.warning("subagent.profile_invalid path={}", path)
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("description"), str):
        logger.warning("subagent.profile_invalid path={} reason=missing description", path)
        return None
    timeout = meta.get("timeout_seconds")
    return SubagentProfile(
        name=str(meta.get("name") or path.stem).strip(),
        description=meta["description"].strip(),
        instructions="\n".join(lines[end + 1 :]).strip(),
        location=path.resolve(),
        model=_optional_str(meta.get("model")),
        tools=_optional_names(meta.get("tools")),
        skills=_optional_names(meta.get("skills")),
        reasoning_effort=_optional_str(meta.get("reasoning_effort")),
        timeout_seconds=timeout if isinstance(timeout, int) and timeout > 0 else None,
    )


def _optional_str(value: object) -> str | None:
    return str(value).strip() or None if value is not None else None


def _optional_names(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [name.strip() for name in value.split(",") if name.strip()]
    if isinstance(value, list):
        return [str(name).strip() for name in value if str(name).strip()]
    return None


def render_profiles_prompt(profiles: Collection[SubagentProfile]) -> str:
    if not profiles:
        return ""
    lines = "\n".join(f"- {profile.name}: {profile.description}" for profile in profiles)
    return f"<available_subagents>\nPass one as `agent` to the subagent tool.\n{lines}\n</available_subagents>"


# ---------------------------------------------------------------- tools


def _get_agent(context: ToolContext) -> BubAgent:
    if "_runtime_agent" not in context.state:
        raise RuntimeError("no runtime agent found in tool context")
    return cast("BubAgent", context.state["_runtime_agent"])


def _child_tool_names(agent: BubAgent, state: TurnState, requested: list[str] | None) -> set[str]:
    """Resolve the child's tools within the parent's: a child never gets a tool its parent lacks."""
    from bub.builtin.tools import ALLOWED_TOOLS_STATE_KEY, resolve_tool_names

    parent_allowed = state.get(ALLOWED_TOOLS_STATE_KEY)
    available = [name for name in agent.tools if parent_allowed is None or name in parent_allowed]
    # Children cannot start or manage subagents.
    excluded = [name for name in available if name == SUBAGENT_TOOL_NAME or name.startswith("subagent.")]
    return resolve_tool_names(requested or None, exclude=excluded, all_names=available)


def _child_skill_names(state: TurnState, requested: list[str] | None) -> list[str] | None:
    parent_allowed = state.get("allowed_skills")
    if requested is None:
        return None if parent_allowed is None else list(parent_allowed)
    names = [name.casefold() for name in requested]
    return names if parent_allowed is None else [name for name in names if name in parent_allowed]


async def _resolve_session(param_session: str, context: ToolContext) -> tuple[str, Tape | None]:
    session = param_session.strip() or "temp"
    if session not in ("fork", "inherit"):
        return session, None
    if context.tape is None:
        return "temp", None
    return "fork", await snapshot_tape(context.tape, context.step_messages)


async def snapshot_tape(tape: Tape, step_messages: Iterable[republic.Message] = ()) -> Tape:
    """Copy ``tape`` and its sidecar tapes into a private store, then add ``step_messages``.

    A forked child starts from the conversation as it is now, including the messages of the
    model step that called it, and does not see what the parent records later.
    """
    from bub.core.sidecars import sidecar_tape_name
    from bub.core.store import InMemoryTapeStore
    from bub.core.tape import TapeEntry

    store = InMemoryTapeStore()
    for name in (tape.name, *(sidecar_tape_name(tape.name, sidecar.name) for sidecar in tape.sidecars)):
        async with tape.scoped(name).query().stream() as entries:
            async for entry in entries:
                await store.append(name, entry.copy())
    for message in step_messages:
        await store.append(tape.name, TapeEntry.message(message.to_dict()))
    return replace(tape, store=store)


async def _build_request(param: SubAgentInput, context: ToolContext) -> tuple[_RunRequest, SubagentProfile | None]:
    from bub.builtin.settings import load_settings
    from bub.utils import workspace_from_state

    agent = _get_agent(context)
    state = context.state
    profile: SubagentProfile | None = None
    if param.agent:
        profiles = discover_subagent_profiles(workspace_from_state(state))
        profile = profiles.get(param.agent.strip().casefold())
        if profile is None:
            known = ", ".join(sorted(item.name for item in profiles.values())) or "none"
            raise ValueError(f"unknown subagent profile {param.agent!r}; available: {known}")
    session, fork_from = await _resolve_session(param.session, context)
    return (
        _RunRequest(
            agent=agent,
            session=session,
            fork_from=fork_from,
            model=param.model or (profile.model if profile else None),
            reasoning_effort=param.reasoning_effort or (profile.reasoning_effort if profile else None),
            allowed_tools=_child_tool_names(agent, state, param.allowed_tools or (profile.tools if profile else None)),
            allowed_skills=_child_skill_names(
                state, param.allowed_skills if param.allowed_skills is not None else profile.skills if profile else None
            ),
            instructions=profile.instructions if profile else None,
            timeout_seconds=param.timeout_seconds
            or (profile.timeout_seconds if profile else None)
            or load_settings().subagent_timeout_seconds,
            base_state=dict(state),
        ),
        profile,
    )


async def _start(
    request: _RunRequest,
    prompt: str | list[dict],
    *,
    context: ToolContext,
    description: str | None,
    profile: str | None,
) -> SubAgentResult:
    task = await subagent_manager.start(
        request,
        prompt,
        events=getattr(context, "events", None),
        parent_state=context.state,
        description=description,
        profile=profile,
        parent_call_id=getattr(context, "call_id", None),
    )
    return task.result()


async def deliver_subagent_results(event: MessagesPending) -> None:
    """Keep a run going until the subagents it started have reported.

    When a step ends without tool calls, the run would end. If it started subagent tasks
    whose results it has not received, wait for one of them to finish (unless other
    messages already continue the run), then add each finished task's notification so the
    run takes another step with it.
    """
    if not event.step_ended:
        return
    tasks = subagent_manager.unread(event.state)
    if not tasks:
        return
    if not event.messages:
        await subagent_manager.wait([task for task in tasks if not task.done], None, any_task=True)
    for task in tasks:
        if task.done:
            task.delivered = True
            event.messages.append([render_notification(task)])


@tool(name=SUBAGENT_TOOL_NAME, context=True, model=SubAgentInput, renderer=_render_result)
async def run_subagent(param: SubAgentInput, *, context: ToolContext) -> SubAgentResult:
    """Delegate a task to a sub-agent that runs its own loop with this agent's tools, in the background.

    Returns a task ID at once; the sub-agent's answer arrives later as a <subagent-notification>
    message. Start several to work in parallel. The sub-agent only sees `prompt` (and, with
    session='fork', the current conversation), so make the prompt self-contained.
    """
    request, profile = await _build_request(param, context)
    return await _start(
        request,
        param.prompt,
        context=context,
        description=param.description,
        profile=profile.name if profile else None,
    )


@tool(name="subagent.wait", context=True, renderer=_render_task_list)
async def subagent_wait(
    task_ids: list[str] | None = None,
    timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
    *,
    context: ToolContext,
) -> list[SubAgentResult]:
    """Return subagent task results, waiting up to timeout_seconds for them to finish (0 checks without waiting).

    Without task_ids, covers this session's tasks whose results were not read yet.
    """
    if task_ids:
        tasks = [subagent_manager.get(task_id) for task_id in task_ids]
    else:
        session_id = str(context.state.get("session_id") or "")
        tasks = [task for task in subagent_manager.tasks(session_id) if not task.delivered]
    await subagent_manager.wait(tasks, max(0, timeout_seconds))
    return [task.result() for task in tasks]


@tool(name="subagent.cancel", context=True, renderer=_render_task_list)
async def subagent_cancel(task_id: str, *, context: ToolContext) -> list[SubAgentResult]:
    """Stop a running subagent task."""
    task = await subagent_manager.cancel(task_id)
    task.delivered = True
    return [task.result()]


@tool(name="subagent.send", context=True, renderer=_render_result)
async def subagent_send(task_id: str, message: str, *, context: ToolContext) -> SubAgentResult | str:
    """Send a message to a subagent task.

    A running task receives it before its next step. A finished task with a named session
    continues that conversation as a new background task.
    """
    task = subagent_manager.get(task_id)
    if not task.done:
        if not await subagent_manager.steer(task, message):
            raise RuntimeError(f"cannot deliver messages to subagent task {task.task_id}")
        return f"Message queued for subagent task {task.task_id}; it reads the message before its next step."
    if not task.persistent or task._request is None:
        raise ValueError(
            f"subagent task {task.task_id} has finished and its temporary session is gone; "
            "start a new subagent, and give it a named `session` to keep a conversation you can continue"
        )
    request = task._request
    # Continue the named session under the current turn: its tools and skills bound the child again.
    continued = _RunRequest(
        agent=request.agent,
        session=request.session,
        fork_from=None,
        model=request.model,
        reasoning_effort=request.reasoning_effort,
        allowed_tools=request.allowed_tools & _child_tool_names(request.agent, context.state, None),
        allowed_skills=request.allowed_skills,
        instructions=request.instructions,
        timeout_seconds=request.timeout_seconds,
        base_state=dict(context.state),
    )
    return await _start(
        continued,
        message,
        context=context,
        description=task.description,
        profile=task.profile,
    )


SUBAGENT_TOOLS = (run_subagent, subagent_wait, subagent_cancel, subagent_send)
