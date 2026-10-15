"""Bub's agent kernel: agent loop, Republic model, tools, system prompt and tape.

Nothing here reads settings or plugin hooks. The loop reports its progress as events
to registered handlers; the framework and the builtin agent assemble these components.
"""

from bub.agent.agent import Agent, events_from
from bub.agent.context import default_tape_context
from bub.agent.events import (
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
from bub.agent.model import ChatModelClient, Model, ModelClient
from bub.agent.recorder import TapeRecorder
from bub.agent.system_prompt import PromptContext, PromptSection, SystemPrompt
from bub.agent.tape import Tape, TapeContext, session_tape_name
from bub.agent.tools import Tool, ToolContext, tool

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
