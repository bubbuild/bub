---
name: telegram
description: |
  Telegram Bot skill for sending and editing Telegram messages via Bot API.
  Use when Bub needs to: (1) Send a message to a Telegram user/group/channel,
  (2) Reply to a specific Telegram message with reply_to_message_id,
  (3) Edit an existing Telegram message, or (4) Push proactive Telegram notifications.
  Prefer rich formatted messages (headings, lists, tables, blockquotes, math, collapsible details, captioned media) over plain markdown whenever the content is more than one short paragraph.
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

## Rich Messages (Bot API 10.1+, June 2026) — **PREFERRED**

For any message that benefits from structured formatting — headings, bullet/numbered lists, tables, blockquotes, collapsible `<details>`, captioned media, math blocks, code blocks with language hints, or mixed media — use the dedicated `telegram_rich.py` script with `rich-html-style` markup. This is the **first-choice** path; fall back to plain markdown (`telegram_send.py`) only for short single-paragraph replies.

The Rich Message API uses `sendRichMessage` and `editMessageText` with a `rich_message` payload. Exactly one of `html` or `markdown` must be supplied. Supported HTML tags include `<b>`, `<i>`, `<u>`, `<s>`, `<tg-spoiler>`, `<code>`, `<pre>`, `<mark>`, `<sub>`, `<sup>`, `<h1>`-`<h6>`, `<p>`, `<ul>`/`<ol>`/`<li>`, `<blockquote>`, `<aside>`, `<details>`/`<summary>`, `<table>`, `<figure>`/`<figcaption>`, `<img>`/`<video>`/`<audio>`, `<tg-collage>`, `<tg-slideshow>`, `<tg-map>`, `<tg-math>`, `<tg-math-block>`, `<tg-reference>`, `<tg-emoji>`, `<tg-time>`. Full reference: https://core.telegram.org/bots/api#rich-html-style

```bash
# Send rich message (HTML)
uv run ./scripts/telegram_rich.py \
  --chat-id <CHAT_ID> \
  --html "<h2>Title</h2><p>Body with <b>bold</b> and <code>code</code>.</p>"

# Send rich message (HTML from heredoc via file)
uv run ./scripts/telegram_rich.py \
  --chat-id <CHAT_ID> \
  --html-file /tmp/payload.html \
  --reply-to <MESSAGE_ID>

# Send rich message (Markdown instead of HTML)
uv run ./scripts/telegram_rich.py \
  --chat-id <CHAT_ID> \
  --markdown "## Title\n\n- item one\n- item two"

# Edit an existing message to become a rich message
uv run ./scripts/telegram_rich.py \
  --chat-id <CHAT_ID> \
  --edit <MESSAGE_ID> \
  --html "<p>Updated content.</p>"
```

For multi-line rich HTML, prefer `--html-file` over passing the payload on the command line to avoid shell escaping pitfalls. The script reads the file as UTF-8 and strips a single trailing newline.

## Execution Policy

1. If handling a direct user message in Telegram and `message_id` is known, send a reply message (`--reply-to`).
2. If source metadata says sender is a bot (`sender_is_bot=true`), do not use reply mode, but send a normal message and prefix content with `@<sender_username>` (or the provided source username). If the user doesn't have a username, use `--source-user-id` to mention via `tg://user?id=` link.
3. For long-running tasks, optionally send one progress message, then edit that same message for final status.
4. **ALWAYS pass message content via stdin using heredoc pipe and `--message -` (or `--text -`).** NEVER embed message text directly in shell arguments — special characters like `'`, `"`, `$`, `!` will be mangled or cause syntax errors.
5. **Prefer `telegram_rich.py` (rich-html-style) over `telegram_send.py` for anything beyond a single short paragraph.** See the "Rich Messages" section above.

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

# Source message sender is bot: no direct reply, use @username style
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/telegram_send.py --chat-id <CHAT_ID> --token "$BUB_TELEGRAM_TOKEN" --source-is-bot --source-username <USERNAME> --message -
Message to a bot using @username mention.
EOF

# Source message sender is bot without username: use tg://user?id= link
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/telegram_send.py --chat-id <CHAT_ID> --token "$BUB_TELEGRAM_TOKEN" --source-is-bot --source-user-id <USER_ID> --source-display-name "Display Name" --message -
Message to a user/bot using tg://user?id= mention.
EOF

# Edit an existing message
cat << 'EOF' | uv run ${SKILL_DIR}/scripts/telegram_edit.py --chat-id <CHAT_ID> --token "$BUB_TELEGRAM_TOKEN" --message-id <MESSAGE_ID> --text -
Updated content here.
EOF
```

When sending message to a bot, either use `--reply-to` argument or pass `--source-is-bot` with `--source-username` otherwise the bot will not receive the message.

For other actions that not covered by these scripts, use `curl` to call Telegram Bot API directly with the provided token.

## Script Interface Reference

### `telegram_rich.py` (preferred for structured messages)

- `--chat-id`, `-c`: required
- `--html`: rich message content as HTML
- `--html-file`: read rich message HTML from a file (preferred for multi-line payloads)
- `--markdown`: rich message content as Markdown
- `--markdown-file`: read rich message Markdown from a file
- `--rtl`: render right-to-left
- `--skip-entity-detection`: skip URL/email/mention auto-detection
- `--reply-to`, `-r`: optional message ID to reply to
- `--disable-notification`: send silently
- `--edit MESSAGE_ID`: edit an existing message instead of sending
- `--token`, `-t`: optional (normally not needed)

### `telegram_send.py`

- `--chat-id`, `-c`: required, supports comma-separated ids
- `--message`, `-m`: required (use `-` to read from stdin)
- `--reply-to`, `-r`: optional
- `--token`, `-t`: optional (normally not needed)
- `--source-is-bot`: optional flag, disables reply mode and adds mention prefix
- `--source-username`: optional, uses `@username` style mention when set
- `--source-user-id`: optional, uses `tg://user?id=` link mention when username is not available
- `--source-display-name`: optional, display name for user ID mention (defaults to "User")

### `telegram_edit.py`

- `--chat-id`, `-c`: required
- `--message-id`, `-m`: required
- `--text`, `-t`: required (use `-` to read from stdin)
- `--token`: optional (normally not needed)

### Bot to co-Bot Communication

In Telegram groups, communicate with another bot using only these patterns:

1. Reply directly to the other bot's message when `message_id` is available.
2. Use an explicit command mention such as `/command@OtherBot` when you need to invoke that bot intentionally.
3. Do not assume free-form group text will reach another bot.
