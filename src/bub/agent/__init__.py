"""Bub's agent kernel: agent loop, Republic model, events and system prompt.

Nothing here reads settings or plugin hooks. The loop reports its progress as events
to registered handlers; the framework and the builtin agent assemble these components.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
    "TapeEvent",
    "TapeRecorder",
    "ToolCall",
    "ToolCallDecision",
    "ToolCallResult",
    "default_tape_context",
    "events_from",
]

# Exports are imported on first access, so importing one submodule (such as ``bub.agent.events``
# from ``bub.tools``) does not import the agent loop and the modules it depends on.
_EXPORTS = {
    "AfterLlmCall": "bub.agent.events",
    "AfterToolCall": "bub.agent.events",
    "Agent": "bub.agent.agent",
    "AgentEvent": "bub.agent.events",
    "BeforeLlmCall": "bub.agent.events",
    "BeforeToolCall": "bub.agent.events",
    "ChatCompleted": "bub.agent.events",
    "ChatModelClient": "bub.agent.model",
    "CommandFinished": "bub.agent.events",
    "EventBus": "bub.agent.events",
    "EventHandler": "bub.agent.events",
    "LlmCallDecision": "bub.agent.events",
    "LlmCallRequest": "bub.agent.events",
    "LlmCallResult": "bub.agent.events",
    "Model": "bub.agent.model",
    "ModelClient": "bub.agent.model",
    "PromptContext": "bub.agent.system_prompt",
    "PromptSection": "bub.agent.system_prompt",
    "RunStarted": "bub.agent.events",
    "StepFinished": "bub.agent.events",
    "StepStarted": "bub.agent.events",
    "SystemPrompt": "bub.agent.system_prompt",
    "TapeEvent": "bub.agent.events",
    "TapeRecorder": "bub.agent.recorder",
    "ToolCall": "bub.agent.events",
    "ToolCallDecision": "bub.agent.events",
    "ToolCallResult": "bub.agent.events",
    "default_tape_context": "bub.agent.context",
    "events_from": "bub.agent.agent",
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__} has no attribute {name}")
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value
