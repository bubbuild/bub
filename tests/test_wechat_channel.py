from __future__ import annotations

import base64
from pathlib import Path

import pytest

from bub.channels.message import ChannelMessage
from bub.channels.wechat import WeChatChannel, _message_body, _parse_aes_key


def test_message_body_prefers_text_item() -> None:
    body = _message_body(
        [
            {"type": 2, "image_item": {}},
            {"type": 1, "text_item": {"text": "hello from wechat"}},
        ]
    )

    assert body == "hello from wechat"


def test_message_body_falls_back_to_voice_transcript() -> None:
    body = _message_body(
        [
            {"type": 3, "voice_item": {"text": "voice transcript"}},
        ]
    )

    assert body == "voice transcript"


def test_parse_aes_key_supports_raw_and_ascii_hex() -> None:
    raw_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    raw_base64 = base64.b64encode(raw_key).decode("utf-8")
    hex_base64 = base64.b64encode(raw_key.hex().encode("ascii")).decode("utf-8")

    assert _parse_aes_key(raw_base64) == raw_key
    assert _parse_aes_key(hex_base64) == raw_key


@pytest.mark.asyncio
async def test_wechat_send_text_uses_cached_context_token(tmp_path: Path) -> None:
    channel = WeChatChannel(lambda _message: None)
    state_file = tmp_path / "wechat-session.json"
    state_file.write_text(
        """
        {
          "version": 1,
          "login": {
            "bot_token": "bot-token",
            "base_url": "https://ilink.example.com",
            "cdn_base_url": "https://cdn.example.com"
          },
          "contacts": {
            "user@im.wechat": {
              "context_token": "ctx-token"
            }
          },
          "sync": {
            "get_updates_buf": ""
          }
        }
        """.strip(),
        encoding="utf-8",
    )
    channel._settings.state_file = state_file
    channel._state = {
        "login": {"bot_token": "bot-token", "base_url": "https://ilink.example.com", "cdn_base_url": "https://cdn.example.com"},
        "contacts": {"user@im.wechat": {"context_token": "ctx-token"}},
        "sync": {"get_updates_buf": ""},
    }
    calls: list[dict] = []

    async def fake_send_message(*, base_url: str, token: str, payload: dict) -> None:
        calls.append({"base_url": base_url, "token": token, "payload": payload})

    channel._client.send_message = fake_send_message  # type: ignore[method-assign]

    await channel.send(
        ChannelMessage(
            session_id="wechat:user@im.wechat",
            channel="wechat",
            chat_id="user@im.wechat",
            content="hello",
        )
    )

    assert len(calls) == 1
    request = calls[0]
    assert request["base_url"] == "https://ilink.example.com"
    assert request["token"] == "bot-token"  # noqa: S105
    assert request["payload"]["msg"]["context_token"] == "ctx-token"  # noqa: S105
    assert request["payload"]["msg"]["item_list"][0]["text_item"]["text"] == "hello"


@pytest.mark.asyncio
async def test_wechat_send_text_refreshes_context_token_from_disk(tmp_path: Path) -> None:
    state_file = tmp_path / "wechat-session.json"
    state_file.write_text(
        """
        {
          "version": 1,
          "login": {
            "bot_token": "bot-token",
            "base_url": "https://ilink.example.com",
            "cdn_base_url": "https://cdn.example.com"
          },
          "contacts": {},
          "sync": {
            "get_updates_buf": ""
          }
        }
        """.strip(),
        encoding="utf-8",
    )
    channel = WeChatChannel(lambda _message: None)
    channel._settings.state_file = state_file
    channel._state = {
        "login": {"bot_token": "bot-token", "base_url": "https://ilink.example.com", "cdn_base_url": "https://cdn.example.com"},
        "contacts": {},
        "sync": {"get_updates_buf": ""},
    }
    calls: list[dict] = []

    async def fake_send_message(*, base_url: str, token: str, payload: dict) -> None:
        calls.append({"base_url": base_url, "token": token, "payload": payload})

    channel._client.send_message = fake_send_message  # type: ignore[method-assign]

    state_file.write_text(
        """
        {
          "version": 1,
          "login": {
            "bot_token": "bot-token",
            "base_url": "https://ilink.example.com",
            "cdn_base_url": "https://cdn.example.com"
          },
          "contacts": {
            "user@im.wechat": {
              "context_token": "ctx-from-disk"
            }
          },
          "sync": {
            "get_updates_buf": ""
          }
        }
        """.strip(),
        encoding="utf-8",
    )

    await channel.send(
        ChannelMessage(
            session_id="wechat:user@im.wechat",
            channel="wechat",
            chat_id="user@im.wechat",
            content="hello",
        )
    )

    assert len(calls) == 1
    request = calls[0]
    assert request["payload"]["msg"]["context_token"] == "ctx-from-disk"  # noqa: S105


@pytest.mark.asyncio
async def test_wechat_inbound_message_is_active_and_persists_context_token() -> None:
    received: list[ChannelMessage] = []

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)

    channel = WeChatChannel(on_receive)
    channel._state = {
        "login": {"bot_token": "bot-token", "base_url": "https://ilink.example.com", "cdn_base_url": "https://cdn.example.com"},
        "contacts": {},
        "sync": {"get_updates_buf": ""},
    }

    await channel._handle_inbound(
        {
            "from_user_id": "user@im.wechat",
            "message_id": 123,
            "context_token": "ctx-inbound",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        }
    )

    assert len(received) == 1
    message = received[0]
    assert message.is_active is True
    assert message.content == "hello"
    assert channel._state["contacts"]["user@im.wechat"]["context_token"] == "ctx-inbound"  # noqa: S105
