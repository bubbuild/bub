---
name: telegram
description: |
  Telegram Bot skill for sending and editing Telegram messages via Bot API.
  Use when Bub needs to: (1) Send a message to a Telegram user/group/channel,
  (2) Reply to a specific Telegram message with reply_to_message_id,
  (3) Edit an existing Telegram message, or (4) Push proactive Telegram notifications
  when working outside an active Telegram session.
metadata:
  channel: telegram
---

# Telegram Skill

Agent-facing execution guide for Telegram outbound communication.

# Load environment variables from .env first
set -a; source .env 2>/dev/null || true; set +a

Assumption: `BUB_TELEGRAM_TOKEN` is already available.

## Required Inputs
Collect these before execution:

- `chat_id` (required)
- `message_id` (required for edit or reply when source is not a bot)
- message content (required for send/edit)
- `reply_to_message_id` (required for threaded reply behavior)

## Execution Policy

1. If handling a direct user message in Telegram and `message_id` is known, send a reply message (`--reply-to`).
2. If source metadata says sender is a bot (`sender_is_bot=true`), do not use reply mode, but send a normal message and prefix content with `@<sender_username>` (or the provided source username). If the user doesn't have a username, use `--source-user-id` to mention via `tg://user?id=` link.
3. For long-running tasks, optionally send one progress message, then edit that same message for final status.
4. For multi-line text, pass the content via heredoc command substitution instead of embedding raw line breaks in quoted strings.
5. Avoid emitting HTML tags in message content; use Markdown for formatting instead.

## Active Response Policy

When this skill is in scope, prefer proactive and timely Telegram updates:

- Send an immediate acknowledgment for newly assigned tasks
- Send progress updates for long-running operations using message edits
- Send completion notifications when work finishes
- Send important status or failure notifications without waiting for follow-up prompts
- If execution is blocked or fails, send a problem report immediately with cause, impact, and next action

Recommended pattern:

1. Send a short acknowledgment reply
2. Continue processing
3. If blocked, edit or send an issue update immediately
4. Edit the acknowledgment message with final result when possible

## Voice Message Policy

When the inbound Telegram message is voice:

1. Transcribe the voice input first (use STT skill if available)
2. Prepare response content based on transcription
3. Prefer voice response output (use TTS skill if available)
4. If voice output is unavailable, send a concise text fallback and state limitation

## Reaction Policy

When an inbound Telegram message warrants acknowledgment but does not merit a full reply, use a Telegram reaction as the response.
But when any explanation or details are needed, use a normal reply instead.

## Command Templates

Paths are relative to this skill directory.

```bash
# Send simple text with SINGLE quotes
uv run ${SKILL_DIR}/scripts/telegram_send.py \
  --chat-id <CHAT_ID> \
  --message '<TEXT>'

# Or, send multi-line message using heredoc and double quotes
uv run ${SKILL_DIR}/scripts/telegram_send.py \
  --chat-id <CHAT_ID> \
  --message "$(cat <<'EOF'
Build finished successfully.
Summary:
- 12 tests passed
- 0 failures
EOF
)"

# Send reply to a specific message
uv run ${SKILL_DIR}/scripts/telegram_send.py \
  --chat-id <CHAT_ID> \
  --message '<TEXT>' \
  --reply-to <MESSAGE_ID>

# Source message sender is bot: no direct reply, use @username style
uv run ${SKILL_DIR}/scripts/telegram_send.py \
  --chat-id <CHAT_ID> \
  --message '<TEXT>' \
  --source-is-bot \
  --source-username <USERNAME>

# Source message sender is bot without username: use tg://user?id= link
uv run ${SKILL_DIR}/scripts/telegram_send.py \
  --chat-id <CHAT_ID> \
  --message '<TEXT>' \
  --source-is-bot \
  --source-user-id <USER_ID> \
  --source-display-name "Display Name"

# Edit existing message
uv run ${SKILL_DIR}/scripts/telegram_edit.py \
  --chat-id <CHAT_ID> \
  --message-id <MESSAGE_ID> \
  --text '<TEXT>'
```

For other actions that not covered by these scripts, use `curl` to call Telegram Bot API directly with the provided token.

## Script Interface Reference

### `telegram_send.py`

- `--chat-id`, `-c`: required, supports comma-separated ids
- `--message`, `-m`: required
- `--reply-to`, `-r`: optional
- `--token`, `-t`: optional (normally not needed)
- `--source-is-bot`: optional flag, disables reply mode and adds mention prefix
- `--source-username`: optional, uses `@username` style mention when set
- `--source-user-id`: optional, uses `tg://user?id=` link mention when username is not available
- `--source-display-name`: optional, display name for user ID mention (defaults to "User")

### `telegram_edit.py`

- `--chat-id`, `-c`: required
- `--message-id`, `-m`: required
- `--text`, `-t`: required
- `--token`: optional (normally not needed)
