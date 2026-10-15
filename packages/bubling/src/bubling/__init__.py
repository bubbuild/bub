"""Bub's agent kernel: agent loop, Republic model, tools, system prompt and tape.

Nothing here reads settings or plugin hooks. The loop reports its progress as events
to registered handlers; the framework and the builtin agent assemble these components.
"""

from bubling.agent import Agent, events_from
from bubling.context import default_tape_context
from bubling.events import (
    AfterLlmCall,
    AfterToolCall,
    AgentEvent,
    BeforeLlmCall,
    BeforeToolCall,
    ChatCompleted,
    CommandFinished,
    EventBus,
    EventHandler,
    LlmCallDecision,
    LlmCallRequest,
    LlmCallResult,
    RunStarted,
    StepFinished,
    StepStarted,
    TapeEvent,
    ToolCall,
    ToolCallDecision,
    ToolCallResult,
)
from bubling.model import ChatModelClient, Model, ModelClient
from bubling.recorder import TapeRecorder
from bubling.system_prompt import PromptContext, PromptSection, SystemPrompt
from bubling.tape import Tape, TapeContext, session_tape_name
from bubling.tools import Tool, ToolContext, tool

__all__ = [
    "AfterLlmCall",
    "AfterToolCall",
    "Agent",
    "AgentEvent",
    "BeforeLlmCall",
    "BeforeToolCall",
    "ChatCompleted",
    "ChatModelClient",
    "CommandFinished",
    "EventBus",
    "EventHandler",
    "LlmCallDecision",
    "LlmCallRequest",
    "LlmCallResult",
    "Model",
    "ModelClient",
    "PromptContext",
    "PromptSection",
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
    "ToolCallResult",
    "ToolContext",
    "default_tape_context",
    "events_from",
    "session_tape_name",
    "tool",
]
