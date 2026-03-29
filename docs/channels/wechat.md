# WeChat

WeChat is the builtin remote channel adapter for Tencent's public iLink bot protocol.

## Authentication

Login is handled through the official QR flow:

```bash
uv run bub login wechat
```

After scanning, the QR page itself may not redirect. Bub reports scan and confirm progress in the terminal and finishes when the login is confirmed.

## Configuration

Environment variables are read by `WeChatSettings` (`src/bub/channels/wechat.py`).

Optional allowlists (comma-separated):

```bash
BUB_WECHAT_ALLOW_USERS=user@im.wechat,another@im.wechat
BUB_WECHAT_ALLOW_CHATS=user@im.wechat
```

Optional protocol tuning:

```bash
BUB_WECHAT_STATE_FILE=~/.local/state/bub/wechat-session.json
BUB_WECHAT_POLL_INTERVAL_SECONDS=1.0
BUB_WECHAT_API_BASE_URL=https://ilinkai.weixin.qq.com
BUB_WECHAT_CDN_BASE_URL=https://novac2c.cdn.weixin.qq.com/c2c
```

## Message Behavior

- Session id is `wechat:<from_user_id>`.
- WeChat is treated as a debounced channel.
- Inbound text prefers text items and falls back to voice transcript text when present.
- Inbound media downloads images, voice/audio, video, and files into the channel temp directory before handing them to Bub.
- Typing indicators are exposed when the upstream account returns a `typing_ticket`.

## Outbound Behavior

- Empty outbound text is ignored.
- Outbound text and media are both supported.
- Context tokens are cached per contact and reused for downstream replies.
- Media encryption/decryption currently uses the local `openssl` binary (`aes-128-ecb`), so `openssl` must be available on the host.

## Access Control

- If `BUB_WECHAT_ALLOW_CHATS` is set, non-listed chats are ignored.
- If `BUB_WECHAT_ALLOW_USERS` is set, non-listed users are ignored.

## State File

By default, session state is stored at:

```text
~/.local/state/bub/wechat-session.json
```
