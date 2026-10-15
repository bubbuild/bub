"""System prompts assembled from registered sections."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from bub.prompt import UserContent
from bub.turn import TurnState

if TYPE_CHECKING:
    from bub.core.agent import Agent
    from bub.core.tape import Tape
    from bub.core.tools import Tool


@dataclass(frozen=True)
class PromptContext:
    """What a system prompt section sees when rendered for one model step."""

    agent: Agent
    tape: Tape
    prompt: list[UserContent]
    """The step's new user message; empty when the step continues the conversation."""
    tools: list[Tool]
    """Model-facing tools offered in this step."""
    model: str
    """The ``provider:model`` spec called in this step."""

    @property
    def state(self) -> TurnState:
        """The run's turn state."""
        return self.tape.context.state

    @property
    def available_tools(self) -> list[Tool]:
        """All tools the agent was given, before per-step selection."""
        return list(self.agent.tools.values())


PromptSection = str | Callable[[PromptContext], str | None | Awaitable[str | None]]
"""Fixed text, or a function of the step's context returning text (empty or None to skip)."""

S = TypeVar("S", bound=PromptSection)


class SystemPrompt:
    """Ordered system prompt sections, rendered for each model step and joined by blank lines."""

    def __init__(self, sections: Iterable[PromptSection] = ()) -> None:
        self._sections: list[PromptSection] = list(sections)

    @classmethod
    def of(cls, value: SystemPrompt | PromptSection | Iterable[PromptSection]) -> SystemPrompt:
        """Build a system prompt from one section, sections, or a copy of another system prompt."""
        if isinstance(value, SystemPrompt):
            return cls(value.sections)
        if isinstance(value, str) or callable(value):
            return cls([value])
        return cls(value)

    @property
    def sections(self) -> tuple[PromptSection, ...]:
        return tuple(self._sections)

    def add(self, section: S) -> S:
        """Append a section and return it, so this also works as a decorator::

        @system_prompt.add
        def tools(context: PromptContext) -> str: ...
        """
        self._sections.append(section)
        return section

    def remove(self, section: PromptSection) -> None:
        """Remove a section; do nothing if it is not present."""
        with suppress(ValueError):
            self._sections.remove(section)

    async def render(self, context: PromptContext) -> str:
        blocks: list[str] = []
        for section in list(self._sections):
            text = section if isinstance(section, str) else section(context)
            if inspect.isawaitable(text):
                text = await text
            if text:
                blocks.append(text)
        return "\n\n".join(blocks)
