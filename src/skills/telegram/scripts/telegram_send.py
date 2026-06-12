#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
#     "telegramify-markdown>=0.5.0",
# ]
# ///

"""
Telegram Bot Message Sender

A simple script to send messages via Telegram Bot API.
Uses telegramify_markdown to convert markdown to Telegram MarkdownV2 format.
"""

import argparse
import os
import sys

import requests

try:
    from telegramify_markdown import markdownify
except ImportError:
    print("❌ Error: telegramify_markdown not installed. Run: pip install telegramify-markdown")
    sys.exit(1)


def unescape_newlines(text: str) -> str:
    """
    Convert escaped newline sequences to real newlines.
    Handles \\n -> \n, \\r\\n -> \r\n, etc.
    """
    # First unescape \\n to real newline
    result = text.replace("\\n", "\n")
    result = result.replace("\\r\\n", "\r\n")
    result = result.replace("\\r", "\r")
    return result


def edit_message(bot_token: str, chat_id: str, message_id: int, text: str) -> dict:
    """
    Edit an existing message via Telegram Bot API.

    Uses telegramify_markdown to convert text to MarkdownV2 format.

    Args:
        bot_token: Telegram bot token
        chat_id: Target chat ID
        message_id: ID of the message to edit
        text: New message text (will be converted to MarkdownV2)

    Returns:
        API response as dict
    """
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"

    # Convert markdown to Telegram MarkdownV2 format
    converted_text = markdownify(text)

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": converted_text,
        "parse_mode": "MarkdownV2",
    }

    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()

    return response.json()


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_to_message_id: int | None = None,
) -> dict:
    """
    Send a message via Telegram Bot API.

    Uses telegramify_markdown to convert text to MarkdownV2 format.

    Args:
        bot_token: Telegram bot token
        chat_id: Target chat ID
        text: Message text (will be converted to MarkdownV2)
        reply_to_message_id: Optional message ID to reply to

    Returns:
        API response as dict
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Unescape \\n sequences to real newlines (bash/argparse converts real newlines to \\n)
    text = unescape_newlines(text)

    # Convert markdown to Telegram MarkdownV2 format
    converted_text = markdownify(text).rstrip("\n")

    payload = {
        "chat_id": chat_id,
        "text": converted_text,
        "parse_mode": "MarkdownV2",
    }

    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    response = requests.post(url, json=payload, timeout=30)
    if response.status_code == 400 and reply_to_message_id:
        payload.pop("reply_to_message_id", None)
        response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()

    return response.json()


def escape_markdown_v2(text: str) -> str:
    """
    Escape special characters for Telegram MarkdownV2 format.
    """
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join('\\' + char if char in escape_chars else char for char in text)


def main():
    parser = argparse.ArgumentParser(description="Send messages via Telegram Bot API (auto-converts to MarkdownV2)")
    parser.add_argument("--chat-id", "-c", required=True, help="Target chat ID")
    parser.add_argument(
        "--message",
        "-m",
        required=True,
        help="Message text to send (markdown supported, will be converted to MarkdownV2)",
    )
    parser.add_argument("--token", "-t", help="Bot token (defaults to BUB_TELEGRAM_TOKEN env var)")
    parser.add_argument("--reply-to", "-r", type=int, help="Message ID to reply to (creates threaded conversation)")
    parser.add_argument(
        "--source-is-bot",
        action="store_true",
        help="Set when source message sender is a bot; disables reply mode and switches to @username style send",
    )
    parser.add_argument(
        "--source-username",
        help="Source username for @username prefix when --source-is-bot is enabled",
    )
    parser.add_argument(
        "--source-user-id",
        help="Source user ID for mention when username is not available (uses tg://user?id= link)",
    )
    parser.add_argument(
        "--source-display-name",
        help="Display name for user ID mention (defaults to 'User')",
    )

    args = parser.parse_args()

    # Get bot token
    bot_token = args.token or os.environ.get("BUB_TELEGRAM_TOKEN")
    if not bot_token:
        print("❌ Error: Bot token required. Set BUB_TELEGRAM_TOKEN env var or use --token")
        sys.exit(1)

    # Parse chat IDs
    chat_id = args.chat_id.strip()
    reply_to = args.reply_to

    # Read message from stdin if "-" was passed, otherwise use the inline value.
    message = sys.stdin.read() if args.message == "-" else args.message

    # Handle source-is-bot mode: prefix the message with a mention so the bot picks it up.
    if args.source_is_bot and not reply_to and not message.startswith("/"):
        if args.source_user_id:
            # Use tg://user?id= link for mention (works without username)
            display_name = args.source_display_name or "User"
            escaped_name = escape_markdown_v2(display_name)
            mention = f"[{escaped_name}](tg://user?id={args.source_user_id})"
            message = f"{mention}\n\n{message}"
        elif args.source_username:
            # Fall back to explicit /bot@username command mention
            message = f"/bot@{args.source_username} {message}"
        else:
            print("❌ Error: --source-username or --source-user-id is required when --source-is-bot is set without --reply-to")
            sys.exit(1)

    # Send messages
    try:
        send_message(bot_token, chat_id, message, reply_to)
        print(f"✅ Message sent successfully to {chat_id} (MarkdownV2)")
    except requests.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        print(f"   Response: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
