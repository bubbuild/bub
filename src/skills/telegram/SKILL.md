---
name: telegram
description: |
  Telegram Bot skill for sending and editing Telegram messages via Bot API.
  Use when Bub needs to: (1) Send a message to a Telegram user/group/channel,
  (2) Reply to a specific Telegram message with reply_to_message_id,
  (3) Edit an existing Telegram message, or (4) Push proactive Telegram notifications.
metadata:
  channel: telegram
---

# Telegram Skill

Agent-facing execution guide for Telegram outbound communication.

Env vars:

- `BUB_TELEGRAM_TOKEN=${config.telegram.token}`

## Required Inputs
Collect these before execution:

- `chat_id` (required)
- `message_id` (required for edit or reply)
- message content (required for send/edit)
- `reply_to_message_id` (required when you need a threaded reply)

## Execution Policy

1. If handling a Telegram message and `message_id` is known, send a reply message with `--reply-to`.
2. If there is no message to reply to, send a normal message to `chat_id`.
3. For long-running tasks, optionally send one progress message, then edit that same message for final status.
4. **ALWAYS pass message content via stdin using heredoc pipe and `--message -` (or `--text -`).** NEVER embed message text directly in shell arguments — special characters like `'`, `"`, `$`, `!` will be mangled or cause syntax errors.
5. Avoid emitting HTML tags in message content; use Markdown for formatting instead.

## Bot to co-Bot Communication

In Telegram groups, communicate with another bot using only these patterns:

1. Reply directly to the other bot's message when `message_id` is available.
2. Use an explicit command mention such as `/command@OtherBot` when you need to invoke that bot intentionally.
3. Do not assume free-form group text will reach another bot.

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
# Send message (ALWAYS use heredoc stdin, never inline text in arguments)
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/telegram_send.py --chat-id <CHAT_ID> --token "$BUB_TELEGRAM_TOKEN" --message -
Your message content here.
Special characters are safe: $100, "quotes", 'apostrophes', !exclamation
EOF

# Reply to a specific message
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/telegram_send.py --chat-id <CHAT_ID> --token "$BUB_TELEGRAM_TOKEN" --reply-to <MESSAGE_ID> --message -
Reply content here.
EOF

# Edit an existing message
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/telegram_edit.py --chat-id <CHAT_ID> --token "$BUB_TELEGRAM_TOKEN" --message-id <MESSAGE_ID> --text -
Updated content here.
EOF
```

When sending message to a bot, either use `--reply-to` argument or pass `--source-is-bot` with `--source-username` otherwise the bot will not receive the message.

For other actions that not covered by these scripts, use `curl` to call Telegram Bot API directly with the provided token.

## Script Interface Reference

### `telegram_send.py`

- `--chat-id`, `-c`: required, supports comma-separated ids
- `--message`, `-m`: required (use `-` to read from stdin)
- `--reply-to`, `-r`: optional
- `--token`, `-t`: optional (normally not needed)

### `telegram_edit.py`

- `--chat-id`, `-c`: required
- `--message-id`, `-m`: required
- `--text`, `-t`: required (use `-` to read from stdin)
- `--token`: optional (normally not needed)
