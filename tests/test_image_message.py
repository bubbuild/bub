"""Tests for image/media message handling through the pipeline."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from bub.builtin.hook_impl import BuiltinImpl
from bub.channels.message import ChannelMessage, MediaItem
from bub.channels.telegram import TelegramChannel, TelegramMessageParser, _extract_media_items
from bub.framework import BubFramework

# ---------------------------------------------------------------------------
# MediaItem & ChannelMessage
# ---------------------------------------------------------------------------


def test_media_item_keeps_fetcher_and_filename() -> None:
    async def fetch_bytes() -> bytes:
        return b"abc"

    item = MediaItem(type="image", mime_type="image/jpeg", filename="a.jpg", data_fetcher=fetch_bytes)

    assert item.type == "image"
    assert item.mime_type == "image/jpeg"
    assert item.filename == "a.jpg"
    assert item.data_fetcher is fetch_bytes


@pytest.mark.asyncio
async def test_media_item_returns_none_when_fetcher_skips_download() -> None:
    item = MediaItem(type="video", mime_type="video/mp4", data_fetcher=_async_return(None))

    assert await item.get_url() is None


def test_channel_message_from_batch_merges_media() -> None:
    m1 = ChannelMessage(
        session_id="s",
        channel="tg",
        content="a",
        media=[MediaItem(type="image", mime_type="image/jpeg", data_fetcher=_async_return(b"AAA"))],
    )
    m2 = ChannelMessage(
        session_id="s",
        channel="tg",
        content="b",
        media=[MediaItem(type="image", mime_type="image/jpeg", data_fetcher=_async_return(b"BBB"))],
    )
    merged = ChannelMessage.from_batch([m1, m2])

    assert merged.content == "a\nb"
    assert len(merged.media) == 2
    assert merged.media[0] is m1.media[0]
    assert merged.media[1] is m2.media[0]


def test_channel_message_from_batch_no_media() -> None:
    m1 = ChannelMessage(session_id="s", channel="tg", content="a")
    m2 = ChannelMessage(session_id="s", channel="tg", content="b")
    merged = ChannelMessage.from_batch([m1, m2])

    assert merged.media == []


# ---------------------------------------------------------------------------
# _extract_media_items
# ---------------------------------------------------------------------------


def test_extract_media_items_from_photo_metadata() -> None:
    metadata = {
        "type": "photo",
        "media": {
            "file_id": "abc",
            "mime_type": "image/jpeg",
            "width": 800,
            "height": 600,
            "data_fetcher": _async_return(b"\xff\xd8\xff\xe0"),
        },
    }
    items = _extract_media_items(metadata)

    assert len(items) == 1
    assert items[0].type == "image"
    assert items[0].mime_type == "image/jpeg"
    assert callable(items[0].data_fetcher)
    assert "data_fetcher" not in metadata["media"]


def test_extract_media_items_from_sticker_metadata() -> None:
    metadata = {
        "type": "sticker",
        "media": {
            "file_id": "stk",
            "mime_type": "image/webp",
            "data_fetcher": _async_return(b"RIFF"),
        },
    }
    items = _extract_media_items(metadata)

    assert len(items) == 1
    assert items[0].type == "image"


def test_extract_media_items_from_audio_metadata() -> None:
    metadata = {
        "type": "audio",
        "media": {
            "file_id": "aud",
            "mime_type": "audio/mpeg",
            "data_fetcher": _async_return(b"\xff\xfb"),
        },
    }
    items = _extract_media_items(metadata)

    assert len(items) == 1
    assert items[0].type == "audio"


def test_extract_media_items_from_video_metadata() -> None:
    metadata = {
        "type": "video",
        "media": {
            "file_id": "vid",
            "mime_type": "video/mp4",
            "data_fetcher": _async_return(b"\x00\x00\x00"),
        },
    }
    items = _extract_media_items(metadata)

    assert len(items) == 1
    assert items[0].type == "video"


@pytest.mark.asyncio
async def test_telegram_video_parser_defaults_to_mp4_mime_type() -> None:
    parser = TelegramMessageParser()
    message = SimpleNamespace(
        caption=None,
        video=SimpleNamespace(
            file_id="vid",
            file_size=None,
            width=640,
            height=480,
            duration=3,
            mime_type=None,
        ),
    )

    content, media = await parser._parse_video(message)  # type: ignore[arg-type]

    assert content == "[Video: 3s]"
    assert media is not None
    assert media["mime_type"] == "video/mp4"
    assert callable(media["data_fetcher"])


@pytest.mark.asyncio
async def test_telegram_audio_parser_defaults_to_mpeg_mime_type() -> None:
    parser = TelegramMessageParser()
    message = SimpleNamespace(
        audio=SimpleNamespace(
            file_id="aud",
            file_size=None,
            duration=3,
            mime_type=None,
            title=None,
            performer=None,
        ),
    )

    content, media = await parser._parse_audio(message)  # type: ignore[arg-type]

    assert content == "[Audio: Unknown (3s)]"
    assert media is not None
    assert media["mime_type"] == "audio/mpeg"
    assert callable(media["data_fetcher"])


def test_extract_media_items_from_document_metadata() -> None:
    metadata = {
        "type": "document",
        "media": {
            "file_id": "doc",
            "mime_type": "application/pdf",
            "data_fetcher": _async_return(b"%PDF"),
        },
    }
    items = _extract_media_items(metadata)

    assert len(items) == 1
    assert items[0].type == "document"


def test_extract_media_items_returns_empty_when_no_media() -> None:
    assert _extract_media_items({"type": "text"}) == []


def test_extract_media_items_returns_empty_when_media_is_none() -> None:
    assert _extract_media_items({"type": "photo", "media": None}) == []


def test_extract_media_items_returns_empty_when_no_data() -> None:
    metadata = {"type": "photo", "media": {"file_id": "abc", "width": 800}}
    assert _extract_media_items(metadata) == []


def test_extract_media_items_unknown_type_defaults_to_document() -> None:
    metadata = {
        "type": "unknown_new_thing",
        "media": {"mime_type": "foo/bar", "data_fetcher": _async_return(b"\x00")},
    }
    items = _extract_media_items(metadata)

    assert items[0].type == "document"


# ---------------------------------------------------------------------------
# TelegramChannel._build_message with media
# ---------------------------------------------------------------------------


def _async_return(value):
    async def runner(*args, **kwargs):
        return value

    return runner


async def _receive_message(_message) -> None:
    return None


@pytest.mark.asyncio
async def test_telegram_build_message_extracts_media_items(monkeypatch: pytest.MonkeyPatch, load_config) -> None:
    load_config("telegram:\n  token: test-token")
    channel = TelegramChannel(_receive_message)
    photo_metadata = {
        "type": "photo",
        "sender_id": "7",
        "media": {
            "file_id": "f1",
            "mime_type": "image/jpeg",
            "data_fetcher": _async_return(b"\xff\xd8\xff\xe0"),
        },
    }
    channel._parser = SimpleNamespace(  # type: ignore[assignment]
        parse=_async_return(("[Photo message]", photo_metadata)),
        get_reply=_async_return(None),
    )
    monkeypatch.setattr("bub.channels.telegram.MESSAGE_FILTER.filter", lambda message: True)

    message = SimpleNamespace(chat_id=42)
    result = await channel._build_message(message)  # type: ignore[arg-type]

    assert len(result.media) == 1
    assert result.media[0].type == "image"
    assert callable(result.media[0].data_fetcher)


@pytest.mark.asyncio
async def test_telegram_build_message_no_media_for_text(monkeypatch: pytest.MonkeyPatch, load_config) -> None:
    load_config("telegram:\n  token: test-token")
    channel = TelegramChannel(_receive_message)
    channel._parser = SimpleNamespace(  # type: ignore[assignment]
        parse=_async_return(("hello", {"type": "text", "sender_id": "7"})),
        get_reply=_async_return(None),
    )
    monkeypatch.setattr("bub.channels.telegram.MESSAGE_FILTER.filter", lambda message: True)

    message = SimpleNamespace(chat_id=42)
    result = await channel._build_message(message)  # type: ignore[arg-type]

    assert result.media == []


# ---------------------------------------------------------------------------
# build_prompt with media
# ---------------------------------------------------------------------------


class FakeAgent:
    def __init__(self, home: Path) -> None:
        self.settings = SimpleNamespace(home=home)


def _build_impl(tmp_path: Path) -> tuple[BubFramework, BuiltinImpl]:
    framework = BubFramework()
    impl = BuiltinImpl(framework)
    impl._agent = FakeAgent(tmp_path)
    return framework, impl


@pytest.mark.asyncio
async def test_build_prompt_returns_string_without_media(tmp_path: Path) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(session_id="s", channel="tg", content="hello")

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, str)
    assert "hello" in result


@pytest.mark.asyncio
async def test_build_prompt_returns_multimodal_parts_with_image_media(tmp_path: Path) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="tg",
        content="describe this",
        media=[MediaItem(type="image", mime_type="image/jpeg", data_fetcher=_async_return(b"\xff\xd8"))],
    )

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, list)
    assert len(result) == 2

    text_part = result[0]
    assert text_part["type"] == "text"
    assert "describe this" in text_part["text"]

    image_part = result[1]
    assert image_part["type"] == "image_url"
    expected = base64.b64encode(b"\xff\xd8").decode("utf-8")
    assert image_part["image_url"]["url"] == f"data:image/jpeg;base64,{expected}"


@pytest.mark.asyncio
async def test_build_prompt_with_multiple_images(tmp_path: Path) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="tg",
        content="compare these",
        media=[
            MediaItem(type="image", mime_type="image/jpeg", data_fetcher=_async_return(b"A")),
            MediaItem(type="image", mime_type="image/jpeg", data_fetcher=_async_return(b"B")),
        ],
    )

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, list)
    assert len(result) == 3
    assert result[1]["type"] == "image_url"
    assert result[2]["type"] == "image_url"


@pytest.mark.asyncio
async def test_build_prompt_returns_video_url_part_with_video_media(tmp_path: Path) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="tg",
        content="describe this video",
        media=[MediaItem(type="video", mime_type="video/mp4", data_fetcher=_async_return(b"video"))],
    )

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["type"] == "text"
    assert "describe this video" in result[0]["text"]
    expected = base64.b64encode(b"video").decode("utf-8")
    assert result[1] == {
        "type": "video_url",
        "video_url": {"url": f"data:video/mp4;base64,{expected}"},
    }


@pytest.mark.asyncio
async def test_build_prompt_skips_video_when_download_is_too_large(tmp_path: Path) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="tg",
        content="describe this video",
        media=[MediaItem(type="video", mime_type="video/mp4", data_fetcher=_async_return(None))],
    )

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, str)
    assert "describe this video" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime_type", "expected_format"),
    [("audio/mpeg", "mp3"), ("audio/ogg", "ogg"), ("audio/x-wav", "wav")],
)
async def test_build_prompt_returns_input_audio_part(tmp_path: Path, mime_type: str, expected_format: str) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="tg",
        content="listen to this",
        media=[MediaItem(type="audio", mime_type=mime_type, data_fetcher=_async_return(b"audio"))],
    )

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, list)
    assert result[0]["type"] == "text"
    assert "listen to this" in result[0]["text"]
    assert result[1] == {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(b"audio").decode("utf-8"),
            "format": expected_format,
        },
    }


@pytest.mark.asyncio
async def test_build_prompt_skips_remote_audio_url(tmp_path: Path) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="tg",
        content="listen to this",
        media=[MediaItem(type="audio", mime_type="audio/ogg", url="https://example.com/audio.ogg")],
    )

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, str)
    assert "listen to this" in result


@pytest.mark.asyncio
async def test_build_prompt_command_ignores_media(tmp_path: Path) -> None:
    _, impl = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="tg",
        content=",help",
        media=[MediaItem(type="image", mime_type="image/jpeg", data_fetcher=_async_return(b"X"))],
    )

    result = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(result, str)
    assert result == ",help"
    assert message.kind == "command"


# ---------------------------------------------------------------------------
# _extract_text_from_parts
# ---------------------------------------------------------------------------


def test_extract_text_from_parts() -> None:
    from bub.builtin.agent import _extract_text_from_parts

    parts = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,X"}},
        {"type": "text", "text": "world"},
    ]
    assert _extract_text_from_parts(parts) == "hello\nworld"


def test_extract_text_from_parts_empty() -> None:
    from bub.builtin.agent import _extract_text_from_parts

    assert _extract_text_from_parts([]) == ""


def test_extract_text_from_parts_no_text_parts() -> None:
    from bub.builtin.agent import _extract_text_from_parts

    parts = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,X"}}]
    assert _extract_text_from_parts(parts) == ""
