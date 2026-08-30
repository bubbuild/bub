from __future__ import annotations

import json

import pytest

from bub.tape import bub_event, event_extension
from bub.tape_codec import CloudEventJsonTapeCodec, TapeCodecError


def test_cloud_event_json_codec_round_trips_a_structured_event_directly() -> None:
    codec = CloudEventJsonTapeCodec()
    event = bub_event("diagnostic", {"owner": "human"}, model_call_id="model-1")

    decoded = codec.decode(codec.encode(event))
    payload = json.loads(codec.encode(event))

    assert decoded.get_attributes() == event.get_attributes()
    assert decoded.get_data() == event.get_data()
    assert event_extension(decoded, "model_call_id") == "model-1"
    assert payload["specversion"] == "1.0"
    assert payload["data"]["source"] == "system"
    assert "event" not in payload
    assert "cursor" not in payload


def test_cloud_event_json_codec_rejects_non_cloud_event_json() -> None:
    invalid = b'{"id":1,"kind":"event","payload":{},"meta":{},"date":"2026-01-01T00:00:00Z"}'

    with pytest.raises(TapeCodecError, match="invalid structured CloudEvent"):
        CloudEventJsonTapeCodec().decode(invalid)
