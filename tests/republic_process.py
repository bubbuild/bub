"""Fresh-process runner/tool/FileTapeStore probe invoked by the integration test."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import aclosing
from importlib.metadata import version
from pathlib import Path

from republic_fixtures import Body, Transport, sdk_transport, settings, tape_at, wire

from bub.builtin.model_runner import ModelRunner
from bub.tools import Tool


async def main(protocol: str, phase: int, directory: Path) -> None:
    import republic

    config = settings(protocol)
    if protocol == "codex":
        import time

        from republic.auth.codex import CodexTokens, write_tokens

        from bub.builtin.auth import codex_token_path

        write_tokens(
            codex_token_path(directory),
            CodexTokens("fixture-access", "fixture-refresh", time.time() + 3600, "acct_fixture"),
        )
        config = settings(protocol, api_key=None, api_base=None, codex_home=directory)
    body = Body(wire("responses" if protocol == "codex" else protocol, tool=phase == 1))
    transport = Transport([body])

    def inspect(value: int) -> str:
        with (directory / "executions.log").open("a") as file:
            file.write(f"{value}\n")
        return f"value={value}"

    root = tape_at(directory)
    with sdk_transport(transport) as clients:
        runner = ModelRunner(config)
        async with root.fork_tape() as tape:
            await tape.ensure_bootstrap_anchor()
            output = runner.run(
                tape=tape,
                model=config.model,
                tools=[Tool.from_callable(inspect)],
                system_prompt="system",
                prompt="inspect once" if phase == 1 else None,
            )
            async with aclosing(output):
                events = [item async for item in output]
        assert len(transport.requests) == 1 and body.closed == 1
        assert all(client.is_closed for client in clients)
    assert (directory / "executions.log").read_text() == "2\n"
    assert "/site-packages/" in republic.__file__
    if phase == 2:
        assert events[-1].data["text"] == "finished"
        payload = transport.payload()
        if protocol in {"responses", "codex"}:
            history = payload["input"]
            if protocol == "codex":
                assert payload["instructions"] == "system"
                history = [None, *history]  # Leading system message becomes instructions.
            assert history[2]["id"] == "reasoning-item" and history[2]["encrypted_content"] == "opaque-reasoning"
            assert history[3]["id"] == "function-item" and history[3]["call_id"] == "call-original"
            assert history[4] == {"type": "function_call_output", "call_id": "call-original", "output": "value=2"}
        else:
            history = payload["messages"]
            assert history[1]["content"][0] == {"type": "thinking", "thinking": "plan", "signature": "sig-opaque"}
            assert history[1]["content"][1]["id"] == "call-original"
            assert history[2]["content"][0]["tool_use_id"] == "call-original"
    report = {
        "pid": os.getpid(),
        "republic_import": republic.__file__,
        "wheel_version": version("republic"),
        "requests": len(transport.requests),
        "payload": transport.payload(),
        "usage": output.usage,
        "events": [{"kind": item.kind, "data": item.data} for item in events],
    }
    (directory / f"phase-{phase}.json").write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])))
