#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
# ]
# ///

"""
Telegram Bot Rich Message Sender

Sends rich formatted messages via the Bot API 10.1 (June 2026) `sendRichMessage`
endpoint using the `rich-html-style` markup.

Use this script as the **preferred** path whenever the message benefits from
structured formatting (headings, lists, tables, blockquotes, collapsible
details, math, captioned figures, etc.). Fall back to `telegram_send.py`
(plain markdown) only for short single-paragraph replies.
"""

import argparse
import json
import os
import sys

import requests


def send_rich_message(
    bot_token: str,
    chat_id: str,
    html: str | None = None,
    markdown: str | None = None,
    is_rtl: bool = False,
    skip_entity_detection: bool = False,
    reply_to_message_id: int | None = None,
    disable_notification: bool | None = None,
) -> dict:
    """
    Call `sendRichMessage` on the Bot API.

    Exactly one of `html` or `markdown` must be supplied.

    Returns:
        API response as dict
    """
    if (html is None) == (markdown is None):
        raise ValueError("Exactly one of --html or --markdown must be supplied")

    url = f"https://api.telegram.org/bot{bot_token}/sendRichMessage"

    rich_message: dict = {}
    if html is not None:
        rich_message["html"] = html
    if markdown is not None:
        rich_message["markdown"] = markdown
    if is_rtl:
        rich_message["is_rtl"] = True
    if skip_entity_detection:
        rich_message["skip_entity_detection"] = True

    payload: dict = {
        "chat_id": chat_id,
        "rich_message": json.dumps(rich_message, ensure_ascii=False),
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    if disable_notification is not None:
        payload["disable_notification"] = disable_notification

    response = requests.post(url, json=payload, timeout=30)
    if response.status_code == 400 and reply_to_message_id is not None:
        # Some channels don't accept threaded replies; retry without it.
        payload.pop("reply_to_message_id", None)
        response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def edit_rich_message(
    bot_token: str,
    chat_id: str,
    message_id: int,
    html: str | None = None,
    markdown: str | None = None,
    is_rtl: bool = False,
) -> dict:
    """Edit an existing message in-place to a rich message."""
    if (html is None) == (markdown is None):
        raise ValueError("Exactly one of --html or --markdown must be supplied")

    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"

    rich_message: dict = {}
    if html is not None:
        rich_message["html"] = html
    if markdown is not None:
        rich_message["markdown"] = markdown
    if is_rtl:
        rich_message["is_rtl"] = True

    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": json.dumps(rich_message, ensure_ascii=False),
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def read_payload_from_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().rstrip("\n")


def main():
    parser = argparse.ArgumentParser(
        description="Send rich formatted messages via Bot API 10.1 sendRichMessage (rich-html-style)",
    )
    parser.add_argument("--chat-id", "-c", required=True, help="Target chat ID")
    parser.add_argument(
        "--html",
        help="Rich message content as HTML. Mutually exclusive with --markdown.",
    )
    parser.add_argument(
        "--markdown",
        help="Rich message content as Markdown. Mutually exclusive with --html.",
    )
    parser.add_argument(
        "--html-file",
        help="Read rich message HTML from a file (useful for heredoc payloads).",
    )
    parser.add_argument(
        "--markdown-file",
        help="Read rich message Markdown from a file (useful for heredoc payloads).",
    )
    parser.add_argument(
        "--rtl",
        action="store_true",
        help="Render the message right-to-left.",
    )
    parser.add_argument(
        "--skip-entity-detection",
        action="store_true",
        help="Skip auto-detection of URLs/emails/mentions in the text.",
    )
    parser.add_argument(
        "--reply-to",
        "-r",
        type=int,
        help="Message ID to reply to (creates threaded conversation).",
    )
    parser.add_argument(
        "--disable-notification",
        action="store_true",
        help="Send silently.",
    )
    parser.add_argument(
        "--edit",
        type=int,
        metavar="MESSAGE_ID",
        help="Edit an existing message instead of sending a new one.",
    )
    parser.add_argument(
        "--token",
        "-t",
        help="Bot token (defaults to $BUB_TELEGRAM_TOKEN env var).",
    )

    args = parser.parse_args()

    bot_token = args.token or os.environ.get("BUB_TELEGRAM_TOKEN")
    if not bot_token:
        print("❌ Error: Bot token required. Set BUB_TELEGRAM_TOKEN env var or use --token")
        sys.exit(1)

    html = args.html
    markdown = args.markdown
    if args.html_file:
        html = read_payload_from_file(args.html_file)
    if args.markdown_file:
        markdown = read_payload_from_file(args.markdown_file)

    if (html is None) == (markdown is None):
        print("❌ Error: supply exactly one of --html, --html-file, --markdown, --markdown-file")
        sys.exit(1)

    try:
        if args.edit is not None:
            result = edit_rich_message(
                bot_token=bot_token,
                chat_id=args.chat_id,
                message_id=args.edit,
                html=html,
                markdown=markdown,
                is_rtl=args.rtl,
            )
            print(f"✅ Rich message edited (message_id = {args.edit})")
        else:
            result = send_rich_message(
                bot_token=bot_token,
                chat_id=args.chat_id,
                html=html,
                markdown=markdown,
                is_rtl=args.rtl,
                skip_entity_detection=args.skip_entity_detection,
                reply_to_message_id=args.reply_to,
                disable_notification=args.disable_notification or None,
            )
            mid = result.get("result", {}).get("message_id")
            print(f"✅ Rich message sent successfully to {args.chat_id} (message_id = {mid})")
    except requests.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        body = e.response.text if e.response is not None else ""
        print(f"   Response: {body[:1000]}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
