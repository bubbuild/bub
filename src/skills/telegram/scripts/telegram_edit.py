#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
#     "telegramify-markdown>=0.5.0",
# ]
# ///

"""
Telegram Bot Message Editor

Edit an existing message via Telegram Bot API.
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

    Args:
        bot_token: Telegram bot token
        chat_id: Target chat ID
        message_id: ID of the message to edit
        text: New message text (will be converted to MarkdownV2)

    Returns:
        API response as dict
    """
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"

    # Unescape \\n sequences to real newlines (bash/argparse converts real newlines to \\n)
    text = unescape_newlines(text)

    # Convert markdown to Telegram MarkdownV2 format
    converted_text = markdownify(text).rstrip("\n")

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": converted_text,
        "parse_mode": "MarkdownV2",
    }

    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()

    return response.json()


def main():
    parser = argparse.ArgumentParser(description="Edit an existing message via Telegram Bot API")
    parser.add_argument("--chat-id", "-c", required=True, help="Target chat ID")
    parser.add_argument("--message-id", "-m", type=int, required=True, help="ID of the message to edit")
    parser.add_argument("--text", "-t", required=True, help="New message text (markdown supported)")
    parser.add_argument("--token", help="Bot token (defaults to BUB_TELEGRAM_TOKEN env var)")

    args = parser.parse_args()

    # Get bot token
    bot_token = args.token or os.environ.get("BUB_TELEGRAM_TOKEN")
    if not bot_token:
        print("❌ Error: Bot token required. Set BUB_TELEGRAM_TOKEN env var or use --token")
        sys.exit(1)

    try:
        text = sys.stdin.read() if args.text == "-" else args.text
        edit_message(bot_token, args.chat_id, args.message_id, text)
        print(f"✅ Message {args.message_id} edited successfully")
    except requests.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        print(f"   Response: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
