from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from any_llm.types.responses import ResponsesParams

from bub.builtin.agent import Agent
from bub.builtin.codex_provider import OpenaiCodexProvider
from bub.framework import BubFramework
from bub.store import FileTapeStore
from bub.tape import InMemoryTapeStore, Tape
from bub.tools import DirectToolCatalog, Tool, ToolContext


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_responses_appends_native_definitions_and_reuses_them_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistent: bool,
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    calls: list[str] = []
    remote = {
        name: Tool.from_callable(lambda name=name: calls.append(name) or name, name=f"catalog.{name}")
        for name in ("first", "second")
    }

    async def discover(name: str, *, context: ToolContext) -> str:
        await context.tape.append_event("catalog.selected", {"name": name}, context=False)
        return f"Loaded {name}"

    class Catalog(DirectToolCatalog):
        async def prepare(self, tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
            entries = await tape.store.fetch_all(tape.context.build_query(tape.query()).kinds("event"))
            loaded = {
                entry.payload["data"]["name"] for entry in entries if entry.payload.get("name") == "catalog.selected"
            }
            return [
                item for item in tools if item.name not in self.tools or item.name in loaded
            ], "Discover tools by name."

    wire: list[ResponsesParams] = []
    replies: list[str | tuple[str, dict[str, Any]]] = [
        ("discover", {"name": "catalog.second"}),
        ("catalog_second", {}),
        ("discover", {"name": "catalog.first"}),
        ("catalog_first", {}),
        "done",
    ]
    monkeypatch.setattr(OpenaiCodexProvider, "_init_client", lambda *a, **k: None)
    client = OpenaiCodexProvider(api_key="fixture")

    async def responses(params: ResponsesParams, **kwargs: Any):
        wire.append(params)
        reply = replies.pop(0)

        async def events():
            if isinstance(reply, tuple):
                name, arguments = reply
                yield SimpleNamespace(
                    type="response.output_item.added",
                    output_index=0,
                    item=SimpleNamespace(
                        type="function_call", id="item", call_id=f"call-{len(wire)}", name=name, arguments=""
                    ),
                )
                yield SimpleNamespace(
                    type="response.function_call_arguments.delta", output_index=0, delta=json.dumps(arguments)
                )
            else:
                yield SimpleNamespace(type="response.output_text.delta", delta=reply)
            yield SimpleNamespace(
                type="response.completed", response=SimpleNamespace(id="reply", created_at=0, usage=None)
            )

        return events()

    monkeypatch.setattr(client, "_aresponses", responses)
    monkeypatch.setattr("bub.builtin.model_runner.should_use_openai_codex_provider", lambda *a, **k: True)
    monkeypatch.setattr("bub.builtin.model_runner.ModelRunner.create_llm_client", lambda *a, **k: client)
    memory_store = InMemoryTapeStore()

    def agent() -> Agent:
        result = Agent(
            framework,
            tools=[Tool.from_callable(discover, context=True)],
            skill_dirs=[],
            tape_store=FileTapeStore(tmp_path / "store") if persistent else memory_store,
        )
        result.add_catalog(Catalog({item.name: item for item in remote.values()}))
        return result

    async def run(instance: Agent, **kwargs: Any) -> None:
        stream = await instance.run_stream(
            session_id="loading", prompt="Use both tools.", model="openai:gpt-5.5", **kwargs
        )
        async for _ in stream:
            pass

    await run(agent())
    assert calls == ["second", "first"]
    assert all(request.tools == wire[0].tools for request in wire)
    assert [item["name"] for item in wire[0].tools or []] == ["discover"]
    loaded = [item for item in wire[3].input if item.get("type") == "additional_tools"]
    assert [item["tools"][0]["name"] for item in loaded] == ["catalog_second", "catalog_first"]
    assert wire[3].input[: len(wire[2].input)] == wire[2].input
    replies.extend([("catalog_second", {}), "done"])
    await run(agent())
    assert calls == ["second", "first", "second"]
    assert [item for item in wire[-1].input if item.get("type") == "additional_tools"] == loaded

    remote["second"].parameters["description"] = "Updated lookup arguments."
    replies.append("done")
    await run(agent())
    assert "catalog_second" in {item["name"] for item in wire[-1].tools or []}
    assert not any(item.get("type") == "additional_tools" for item in wire[-1].input)

    # A narrower scope must not keep earlier definitions callable.
    replies.append("done")
    await run(agent(), allowed_tools=["catalog_second"])
    assert [item["name"] for item in wire[-1].tools or []] == ["catalog_second"]
    assert not any(item.get("type") == "additional_tools" for item in wire[-1].input)
