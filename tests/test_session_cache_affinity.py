from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bub.builtin.agent import BubAgent
from bub.builtin.settings import AgentSettings
from bub.core import Tape, default_tape_context, session_tape_name
from bub.core.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.core.tape import TapeEntry
from tests.model_fakes import ProviderService


def _make_agent(
    store: InMemoryTapeStore | None = None, *, completion_args: dict[str, object] | None = None
) -> BubAgent:
    framework = MagicMock()
    framework.get_tape_store.return_value = store or InMemoryTapeStore()
    framework.build_tape_context.return_value = default_tape_context()
    framework.get_tape_sidecars.return_value = ()
    framework.get_steering_inbox.return_value = None
    framework.get_system_prompt.return_value = ""
    framework.get_tools.return_value = []
    framework.build_state = MagicMock()
    agent = BubAgent(framework)
    agent.settings = AgentSettings(
        model="openai:gpt-5",
        api_key="test-key",
        client_args={"api_format": "chat"},
        completion_args=completion_args or {},
    )
    agent.skill_dirs = []
    return agent


@pytest.mark.asyncio
async def test_session_affinity_and_cache_retention_lifecycle(
    provider_transport: ProviderService, tmp_path: Path
) -> None:
    for _ in range(5):
        provider_transport.reply_chat()

    store = InMemoryTapeStore()
    agent = _make_agent(store)
    state = {"_runtime_workspace": str(tmp_path)}
    tape = agent.tape(session_tape_name("user/s1", tmp_path), state=state)

    # Multi-turn stability on the same session tape.
    for prompt in ("turn 1", "turn 2"):
        stream = await agent.run_stream(session_id="user/s1", prompt=prompt, state=state)
        _ = [event async for event in stream]

    first_key = provider_transport.body(0)["prompt_cache_key"]
    assert len(first_key) == 32
    assert provider_transport.body(1)["prompt_cache_key"] == first_key
    assert first_key == await tape.session_id()

    # Forked run isolates affinity from parent, and rotates after parent reset.
    fork_stream = await agent.run_stream(session_id="sub/worker", prompt="fork 1", state=state, fork_from=tape)
    _ = [event async for event in fork_stream]
    fork_key_before_reset = provider_transport.body(2)["prompt_cache_key"]
    assert fork_key_before_reset != first_key

    # Reset always rotates session_id even when caller supplies custom state.
    await tape.reset(state={"session_id": first_key, "reason": "fresh"})
    rotated_id = await tape.session_id()
    assert rotated_id != first_key

    stream_after_reset = await agent.run_stream(session_id="user/s1", prompt="turn 3", state=state)
    _ = [event async for event in stream_after_reset]
    assert provider_transport.body(3)["prompt_cache_key"] == rotated_id

    fork_stream_after = await agent.run_stream(session_id="sub/worker", prompt="fork 2", state=state, fork_from=tape)
    _ = [event async for event in fork_stream_after]
    fork_key_after_reset = provider_transport.body(4)["prompt_cache_key"]
    assert fork_key_after_reset not in {rotated_id, fork_key_before_reset}


@pytest.mark.asyncio
async def test_legacy_tape_fallback_and_cache_retention_modes(
    provider_transport: ProviderService, tmp_path: Path
) -> None:
    provider_transport.reply_chat()
    provider_transport.reply_chat()
    store = InMemoryTapeStore()
    tape_name = session_tape_name("user/legacy", tmp_path)
    store.append(tape_name, TapeEntry.anchor("session/start", state={"owner": "human"}))

    legacy_tape = Tape(AsyncTapeStoreAdapter(store), default_tape_context(), _name=tape_name)
    legacy_id = await legacy_tape.session_id()
    assert len(legacy_id) == 32
    assert await legacy_tape.session_id() == legacy_id

    agent = _make_agent(store, completion_args={"cache_retention": "long"})
    state = {"_runtime_workspace": str(tmp_path)}
    stream = await agent.run_stream(session_id="user/legacy", prompt="hello", state=state)
    _ = [event async for event in stream]
    assert provider_transport.body(0)["prompt_cache_key"] == legacy_id
    assert provider_transport.body(0)["prompt_cache_retention"] == "24h"

    agent.settings = AgentSettings(
        model="openai:gpt-5",
        api_key="test-key",
        client_args={"api_format": "chat"},
        completion_args={"cache_retention": "none"},
    )
    stream_none = await agent.run_stream(session_id="user/legacy", prompt="no cache", state=state)
    _ = [event async for event in stream_none]
    assert "prompt_cache_key" not in provider_transport.body(1)
    assert "prompt_cache_retention" not in provider_transport.body(1)
