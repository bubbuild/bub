from __future__ import annotations

import json

import pytest
from cloudevents.core.v1.event import CloudEvent

from bub.store import InMemoryTapeStore, TapeQuery
from bub.tape import Tape, TapeContext, event_data, message_event


@pytest.mark.asyncio
async def test_committed_cloud_event_data_is_an_atif_step() -> None:
    store = InMemoryTapeStore()

    first = await store.append("tape", message_event({"role": "user", "content": "check"}))
    second = await store.append(
        "tape",
        message_event(
            {"role": "assistant", "content": ""},
            llm_call_count=1,
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":1}'}}],
            tool_results=[{"answer": 2}],
            model="openai:gpt-test",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "cost_usd": 0.25,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        ),
    )

    first_step = event_data(first.event)
    assert first_step == {
        "step_id": 1,
        "source": "user",
        "message": "check",
        "timestamp": first_step["timestamp"],
    }
    step = event_data(second.event)
    assert step["step_id"] == 2
    assert step["source"] == "agent"
    assert step["tool_calls"] == [{"tool_call_id": "call-1", "function_name": "lookup", "arguments": {"q": 1}}]
    assert step["observation"]["results"] == [
        {"content": json.dumps({"answer": 2}, separators=(",", ":")), "source_call_id": "call-1"}
    ]
    assert step["metrics"] == {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.25, "cached_tokens": 3}
    assert step["llm_call_count"] == 1


@pytest.mark.asyncio
async def test_scan_streams_the_committed_atif_steps_unchanged() -> None:
    store = InMemoryTapeStore()
    committed = await store.append("tape", message_event({"role": "assistant", "content": "done"}))

    scanned = [record async for record in store.scan(TapeQuery("tape"))]

    assert scanned == [committed]
    assert event_data(scanned[0].event)["source"] == "agent"


@pytest.mark.asyncio
async def test_store_rejects_cloud_events_without_atif_data() -> None:
    store = InMemoryTapeStore()
    event = CloudEvent({"source": "https://example.com", "type": "example.invalid"}, {"value": 1})

    with pytest.raises(ValueError, match="ATIF StepObject"):
        await store.append("tape", event)


@pytest.mark.asyncio
async def test_multimodal_messages_persist_as_atif_content_and_rebuild_provider_content() -> None:
    store = InMemoryTapeStore()
    tape = Tape(store, TapeContext(anchor=None)).scoped("tape")
    image = "data:image/png;base64,aW1hZ2U="

    committed = await tape.append(
        message_event({
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        })
    )

    assert event_data(committed.event)["message"] == [
        {"type": "text", "text": "inspect"},
        {"type": "image", "source": {"path": image, "media_type": "image/png"}},
    ]
    assert await tape.read_messages() == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        }
    ]
