from __future__ import annotations

import json

import pytest
from cloudevents.core.formats.json import JSONFormat
from cloudevents.core.v1.event import SPECVERSION_V1_0, CloudEvent

from bub.store import InMemoryTapeStore, TapeQuery
from bub.tape import anchor_event, bub_event, bub_event_type, event_data, event_extension, event_payload, message_event


def test_bub_event_is_a_cloud_event() -> None:
    event = bub_event(
        "diagnostic",
        {"owner": "human"},
        session_id="session-1",
        model_call_id="model-1",
        context=True,
    )

    assert isinstance(event, CloudEvent)
    assert event.get_specversion() == SPECVERSION_V1_0
    assert event.get_source() == "https://bub.build"
    assert event.get_type() == bub_event_type("diagnostic")
    assert event.get_datacontenttype() == "application/json"
    assert event.get_extension("bubkind") is None
    assert event_extension(event, "session_id") == "session-1"
    assert event_extension(event, "model_call_id") == "model-1"
    assert event_extension(event, "context") is False
    assert event_payload(event) == {"owner": "human"}
    assert event_data(event)["source"] == "system"


def test_bub_event_rejects_structured_extension_values() -> None:
    with pytest.raises(TypeError, match="must be a CloudEvents scalar"):
        bub_event("invalid", {}, metadata={"nested": True})


def test_only_messages_and_anchors_are_context_visible_by_default() -> None:
    message = message_event({"role": "user", "content": "hello"})
    anchor = anchor_event("phase/start")

    assert event_extension(message, "context") is True
    assert event_extension(anchor, "context") is True


def test_message_event_rejects_non_conversation_roles() -> None:
    with pytest.raises(ValueError, match="invalid message role"):
        message_event({"role": "tool", "content": "alternate path"})


@pytest.mark.asyncio
async def test_tape_stream_preserves_cloud_event_identity() -> None:
    store = InMemoryTapeStore()
    first = bub_event("one", {"n": 1})
    second = bub_event("two", {"n": 2})
    await store.append("tape", first)
    await store.append("tape", second)

    records = [record async for record in store.scan(TapeQuery("tape"))]
    frames = [json.loads(JSONFormat().write(record.event)) for record in records]

    assert [record.cursor for record in records] == [1, 2]
    assert [record.event.get_id() for record in records] == [first.get_id(), second.get_id()]
    assert [frame["type"] for frame in frames] == [bub_event_type("one"), bub_event_type("two")]
