"""Run code for ``run_code`` in its own process and forward ``tools.*`` calls to Bub.

Bub runs this file with ``python -c`` inside the session's environment, so it must
only use the standard library. Both directions exchange JSON lines:

- Bub writes ``{"type": "run", "code", "filename", "tools"}`` to stdin first,
  then ``{"type": "result", "id", "value"}`` or ``{"type": "error", "id", "message"}``
  for each tool call.
- The runner writes ``{"type": "call", "id", "name", "arguments"}`` to stdout for
  each tool call, ``{"type": "output", "data"}`` for what the code writes to
  ``sys.stdout``, and ends with ``{"type": "done"}`` or
  ``{"type": "failed", "error", "traceback"}``.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import inspect
import io
import itertools
import json
import linecache
import sys
import threading
import time
import traceback
import types
from typing import Any

_OUTPUT_FLUSH_SIZE = 1 << 13
_OUTPUT_FLUSH_INTERVAL = 0.2


class BubError(Exception):
    """A tool call failed or was denied on the Bub side."""


class _Channel:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._out = sys.stdout
        self._send_lock = threading.Lock()
        self._ids = itertools.count()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._first: asyncio.Future[dict[str, Any]] = loop.create_future()

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message) + "\n"
        with self._send_lock:
            self._out.write(line)
            self._out.flush()

    def start(self) -> asyncio.Future[dict[str, Any]]:
        # A daemon thread keeps reading stdin without blocking interpreter exit.
        threading.Thread(target=self._read, daemon=True).start()
        return self._first

    def _read(self) -> None:
        for line in sys.stdin:
            self._loop.call_soon_threadsafe(self._dispatch, json.loads(line))
        self._loop.call_soon_threadsafe(self._close)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not self._first.done():
            self._first.set_result(message)
            return
        future = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if message.get("type") == "result":
            future.set_result(message.get("value"))
        else:
            future.set_exception(BubError(message.get("message", "tool call failed")))

    def _close(self) -> None:
        if not self._first.done():
            self._first.set_exception(EOFError("Bub closed the connection"))
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BubError("Bub closed the connection"))
        self._pending.clear()

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        call_id = next(self._ids)
        future = self._loop.create_future()
        self._pending[call_id] = future
        try:
            self.send({"type": "call", "id": call_id, "name": name, "arguments": arguments})
            return await future
        finally:
            self._pending.pop(call_id, None)


class _Output(io.TextIOBase):
    """Forward what the code prints to Bub in chunks, so output survives a timeout.

    A daemon thread also flushes periodically, because blocking code stalls the event loop.
    """

    def __init__(self, channel: _Channel) -> None:
        self._channel = channel
        self._parts: list[str] = []
        self._size = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._flush_periodically, daemon=True).start()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
            self._size += len(text)
            full = self._size >= _OUTPUT_FLUSH_SIZE
        if full:
            self.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._parts:
                data = "".join(self._parts)
                self._parts.clear()
                self._size = 0
                self._channel.send({"type": "output", "data": data})

    def _flush_periodically(self) -> None:
        while True:
            time.sleep(_OUTPUT_FLUSH_INTERVAL)
            self.flush()


def _tool_function(channel: _Channel, output: _Output, name: str) -> Any:
    async def call(*args: Any, **kwargs: Any) -> Any:
        if args:
            raise TypeError(f"tools.{name}() accepts keyword arguments only")
        output.flush()
        return await channel.call(name, kwargs)

    call.__name__ = name
    return call


async def _main() -> None:
    channel = _Channel(asyncio.get_running_loop())
    run = await channel.start()
    code, filename = run["code"], run["filename"]
    output = _Output(channel)
    sys.stdout = output
    namespace = {
        "__name__": "__run_code__",
        "__builtins__": builtins,
        "tools": types.SimpleNamespace(**{name: _tool_function(channel, output, name) for name in run["tools"]}),
    }
    linecache.cache[filename] = (len(code), None, code.splitlines(keepends=True), filename)
    try:
        compiled = compile(code, filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        # With top-level await enabled, eval returns a coroutine when the code awaits anything.
        result = eval(compiled, namespace)  # noqa: S307
        if inspect.iscoroutine(result):
            await result
    except (Exception, SystemExit) as exc:
        error = traceback.TracebackException.from_exception(exc)
        # Keep only frames from the model's code; runner frames are noise to the model.
        error.stack = traceback.StackSummary.from_list([frame for frame in error.stack if frame.filename == filename])
        output.flush()
        channel.send({"type": "failed", "error": f"{type(exc).__name__}: {exc}", "traceback": "".join(error.format())})
    else:
        output.flush()
        channel.send({"type": "done"})


if __name__ == "__main__":
    asyncio.run(_main())
