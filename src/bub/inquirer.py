from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import typer
from click import get_current_context
from inquirer_textual import prompts
from inquirer_textual.common.Choice import Choice
from inquirer_textual.common.InquirerResult import InquirerResult
from inquirer_textual.common.PromptSettings import PromptSettings
from inquirer_textual.common.Shortcut import Shortcut

from bub.application import PromptChanged, PromptKind

CheckboxValidator = Callable[[list[str]], bool | str]

CHECKBOX_HINT_SETTINGS = PromptSettings(shortcuts=[Shortcut("space", "toggle", "Space check/uncheck")])


def ask_prompt(
    prompt: Callable[..., InquirerResult[Any]], message: str, *, kind: PromptKind = "question", **kwargs: Any
) -> Any:
    from bub.framework import BubFramework

    ctx = get_current_context(silent=True)
    framework = ctx.find_root().obj if ctx else None
    events = framework.events if isinstance(framework, BubFramework) else None
    if events is not None:
        asyncio.run(events.emit(PromptChanged(kind)))
    try:
        question = prompt(message, **kwargs)
        if question.command in {"ctrl+c", "quit"}:
            raise typer.Abort()
        answer = question.value
        if answer is None:
            raise typer.Abort()
        return answer
    finally:
        if events is not None:
            asyncio.run(events.emit(PromptChanged(None)))


def ask_text(message: str, default: str = "") -> str:
    return cast("str", ask_prompt(prompts.text, message, default=default))


def ask_secret(message: str) -> str:
    return cast("str", ask_prompt(prompts.secret, message, kind="auth"))


def ask_confirm(message: str, default: bool = False, *, kind: PromptKind = "question") -> bool:
    return cast("bool", ask_prompt(prompts.confirm, message, default=default, kind=kind))


def ask_select(message: str, choices: list[str], default: str = "") -> str:
    return cast(
        "str",
        ask_prompt(
            prompts.select,
            message,
            choices=cast("list[str | Choice]", choices),
            default=default,
        ),
    )


def ask_fuzzy(message: str, choices: list[str], default: str | None = None) -> str:
    return cast(
        "str",
        ask_prompt(
            prompts.fuzzy,
            message,
            choices=cast("list[str | Choice]", choices),
            default=default,
        ),
    )


def ask_checkbox(
    message: str,
    choices: list[str],
    enabled: list[str] | None = None,
    validate: CheckboxValidator | None = None,
) -> list[str]:
    while True:
        answer: list[str | Choice] = ask_prompt(
            prompts.checkbox,
            message,
            choices=cast("list[str | Choice]", choices),
            enabled=cast("list[str | Choice] | None", enabled),
            settings=CHECKBOX_HINT_SETTINGS,
        )
        values = list(cast("list[str]", answer or []))
        if validate is None:
            return values
        validation_result = validate(values)
        if validation_result is True:
            return values
        typer.secho(str(validation_result), err=True, fg="red")
