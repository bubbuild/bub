"""Bub's agent kernel: agent loop, Republic model, tools, system prompt and tape.

Nothing here reads settings or plugin hooks. The loop reports its progress as events
to registered handlers; the framework and the builtin agent assemble these components.
"""

from bub.core.agent import Agent, events_from
from bub.core.context import default_tape_context
from bub.core.events import (
    AgentEvent,
    ChatFinished,
    CommandFinished,
    EventBus,
    EventHandler,
    LLMCallDecision,
    LLMCallFinished,
    LLMCallRequest,
    LLMCallResult,
    LLMCallStarted,
    MessagesPending,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
    TapeEvent,
    ToolCall,
    ToolCallDecision,
    ToolCallFinished,
    ToolCallResult,
    ToolCallStarted,
)
from bub.core.model import ChatModelClient, Model, ModelClient
from bub.core.recorder import TapeRecorder
from bub.core.system_prompt import PromptContext, PromptSection, SystemPrompt
from bub.core.tape import Tape, TapeContext, session_tape_name
from bub.core.tools import Tool, ToolContext, tool

__all__ = [
    "Agent",
    "AgentEvent",
    "ChatFinished",
    "ChatModelClient",
    "CommandFinished",
    "EventBus",
    "EventHandler",
    "LLMCallDecision",
    "LLMCallFinished",
    "LLMCallRequest",
    "LLMCallResult",
    "LLMCallStarted",
    "MessagesPending",
    "Model",
    "ModelClient",
    "PromptContext",
    "PromptSection",
    "RunFinished",
    "RunStarted",
    "StepFinished",
    "StepStarted",
    "SystemPrompt",
    "Tape",
    "TapeContext",
    "TapeEvent",
    "TapeRecorder",
    "Tool",
    "ToolCall",
    "ToolCallDecision",
    "ToolCallFinished",
    "ToolCallResult",
    "ToolCallStarted",
    "ToolContext",
    "default_tape_context",
    "events_from",
    "session_tape_name",
    "tool",
]
