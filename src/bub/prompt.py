"""User prompt content and the legacy prompt forms accepted for compatibility."""

from __future__ import annotations

from typing import Any

import republic

from bub.errors import BubError, ErrorKind

type UserContent = str | republic.Image | republic.Audio | republic.Video
type LegacyPrompt = str | list[dict[str, Any]]
"""Prompt forms used before ``list[UserContent]``: text, or text and media content blocks."""

_MEDIA_TYPES = (republic.Image, republic.Audio, republic.Video)


def to_content(prompt: list[UserContent] | LegacyPrompt) -> list[UserContent]:
    """Return user content, converting text and legacy content blocks by their value."""
    from bub.tape import to_message

    if isinstance(prompt, str):
        return [prompt]
    if not isinstance(prompt, list):
        raise BubError(ErrorKind.INVALID_INPUT, "Expected user content, text, or a list of content blocks.")
    content: list[UserContent] = []
    for item in prompt:
        if isinstance(item, str | republic.Image | republic.Audio | republic.Video):
            content.append(item)
        elif isinstance(item, dict):
            for part in to_message({"role": "user", "content": [item]}).parts:
                if isinstance(part, republic.Text):
                    content.append(part.text)
                elif isinstance(part, _MEDIA_TYPES):
                    content.append(part)
                else:
                    raise BubError(ErrorKind.INVALID_INPUT, f"Unsupported prompt content block: {item.get('type')}")
        else:
            raise BubError(ErrorKind.INVALID_INPUT, f"Unsupported prompt content: {type(item).__name__}")
    return content


def to_legacy_prompt(content: list[UserContent]) -> LegacyPrompt:
    """Return the legacy form: text without media, otherwise content blocks with media URLs."""
    if all(isinstance(item, str) for item in content):
        return prompt_text(content)
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        else:
            blocks.append({"type": item.kind, "media_type": item.media_type, "url": item.data_url})
    return blocks


def prompt_text(content: list[UserContent]) -> str:
    """Join the text items of a prompt."""
    return "\n".join(item for item in content if isinstance(item, str))
