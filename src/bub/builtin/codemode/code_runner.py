"""Run ``Environment.run_code`` code in a Python process started with ``Environment.spawn``.

The process runs ``code_runner_child`` and talks to Bub over stdin/stdout with
JSON lines; see that module for the protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_core import to_jsonable_python

from bub.builtin.codemode import code_runner_child
from bub.environment import CodeFailed
from bub.errors import BubError, ErrorKind

if TYPE_CHECKING:
    from bub.environment import CallTool, Environment, Process

_STOP_TIMEOUT_SECONDS = 3.0
_CHILD_SOURCE = Path(code_runner_child.__file__).read_text(encoding="utf-8")


class _Session:
    """Drive one child process: send the code, serve its tool calls, and forward its output."""

    def __init__(self, process: Process, call_tool: CallTool, write: Callable[[str], None]) -> None:
        self.process = process
        self.call_tool = call_tool
        self.write = write
        self._write_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._write_lock:
            await self.process.write_stdin(json.dumps(message).encode() + b"\n")

    async def run(self, code: str, filename: str, tools: Sequence[str]) -> dict[str, Any]:
        await self.send({"type": "run", "code": code, "filename": filename, "tools": list(tools)})
        calls: set[asyncio.Task[None]] = set()
        try:
            async with aclosing(_read_lines(self.process.stdout)) as lines:
                async for line in lines:
                    message = _parse_message(line)
                    if message is None:
                        # Text the code wrote around the protocol, e.g. to sys.__stdout__.
                        self.write(line.decode("utf-8", errors="replace") + "\n")
                    elif message["type"] == "output":
                        self.write(str(message.get("data", "")))
                    elif message["type"] == "call":
                        task = asyncio.create_task(self._serve_call(message))
                        calls.add(task)
                        task.add_done_callback(calls.discard)
                    elif message["type"] in ("done", "failed"):
                        return message
        finally:
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
        raise EOFError("code runner exited before reporting a result")

    async def _serve_call(self, message: dict[str, Any]) -> None:
        call_id, name, arguments = message.get("id"), message.get("name"), message.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            await self.send({"type": "error", "id": call_id, "message": f"malformed tool call: {name!r}"})
            return
        try:
            value = await self.call_tool(name, arguments)
        except Exception as exc:
            reply: dict[str, Any] = {"type": "error", "id": call_id, "message": str(exc)}
        else:
            try:
                reply = {"type": "result", "id": call_id, "value": to_jsonable_python(value)}
            except ValueError as exc:
                reply = {"type": "error", "id": call_id, "message": f"tool result is not JSON serializable: {exc}"}
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            # If the runner already exited, run() reports that.
            await self.send(reply)


async def _read_lines(stream: asyncio.StreamReader) -> AsyncGenerator[bytes]:
    """Split a stream into lines without the StreamReader line-length limit; results can be large."""
    buffer = bytearray()
    while chunk := await stream.read(1 << 16):
        buffer += chunk
        while (index := buffer.find(b"\n")) >= 0:
            yield bytes(buffer[:index])
            del buffer[: index + 1]
    if buffer:
        yield bytes(buffer)


def _parse_message(line: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) and isinstance(message.get("type"), str) else None


async def _stop(process: Process) -> None:
    """Kill the runner and anything the code started, and reap it."""
    process.signal(kill=True)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_STOP_TIMEOUT_SECONDS):
            await process.wait()


async def run_code_in_subprocess(
    environment: Environment, code: str, *, tools: Sequence[str], call_tool: CallTool, write: Callable[[str], None]
) -> None:
    """Implement ``Environment.run_code`` for environments with a Python interpreter (``environment.python``).

    The process and everything it started are killed afterwards. Environment plugins can reuse this.
    """
    process = await environment.spawn([environment.python, "-u", "-c", _CHILD_SOURCE])
    session = _Session(process, call_tool, write)
    stderr = asyncio.create_task(process.stderr.read())
    try:
        outcome = await session.run(code, f"<run_code-{uuid.uuid4().hex[:8]}>", tools)
    except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
        await _stop(process)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_STOP_TIMEOUT_SECONDS):
                await asyncio.shield(stderr)
        raise BubError(
            ErrorKind.TOOL,
            f"Code runner exited unexpectedly with code {process.returncode}",
            details={"stderr": stderr.result().decode("utf-8", errors="replace") if stderr.done() else ""},
        ) from exc
    finally:
        await _stop(process)
        stderr.cancel()
        await asyncio.gather(stderr, return_exceptions=True)
    if outcome["type"] == "failed":
        raise CodeFailed(str(outcome.get("error", "")), str(outcome.get("traceback", "")))
