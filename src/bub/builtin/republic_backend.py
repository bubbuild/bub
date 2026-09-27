"""Optional Republic boundary: native model data, with execution/lifecycle in Bub."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from republic import (
    Message,
    ProviderError,
    Request,
    RequestOptions,
    Response,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    events,
    stream,
)
from republic import Tool as ModelTool
from republic.providers.anthropic import AnthropicMessages
from republic.providers.openai import OpenAIChatCompletions, OpenAIResponses

from bub.builtin.context import render_tool_result
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import LlmCallRequest
from bub.streaming import StreamEvent, StreamState
from bub.tape import Tape
from bub.tools import Tool, ToolExecution
from bub.tracing import current_span, event

if TYPE_CHECKING:
    from bub.builtin.model_runner import ModelOutputAccumulator, ModelRunner

Provider = OpenAIChatCompletions | OpenAIResponses | AnthropicMessages
_DEFAULTS = {"openai": "chat", "openrouter": "chat", "anthropic": "messages"}


def protocol_for(settings: AgentSettings, candidate: ModelCandidate) -> str:
    provider = candidate.provider.value
    if settings.republic_protocols.keys() - _DEFAULTS.keys():
        raise BubError(ErrorKind.CONFIG, "Unsupported Republic provider configuration.")
    protocol = settings.republic_protocols.get(provider, _DEFAULTS.get(provider))
    if (provider, protocol) not in {
        ("openai", "chat"),
        ("openai", "responses"),
        ("openrouter", "chat"),
        ("anthropic", "messages"),
    }:
        raise BubError(ErrorKind.CONFIG, f"Republic does not support {provider}/{protocol} in Bub.")
    return f"{provider}.{protocol}"


def create_provider(settings: AgentSettings, candidate: ModelCandidate) -> Provider:
    protocol = protocol_for(settings, candidate)
    if settings.client_args:
        raise BubError(
            ErrorKind.CONFIG, "Republic client_args are unsupported; use the provider factory for injected clients."
        )
    config = settings.model_client_kwargs(candidate.provider)
    if not config["api_key"]:
        raise BubError(
            ErrorKind.CONFIG, "Republic requires an explicit Bub API key; OAuth login discovery is not enabled."
        )
    base_url = config["api_base"]
    if candidate.provider.value == "openrouter" and base_url is None:
        base_url = "https://openrouter.ai/api/v1"
    adapter = (
        AnthropicMessages
        if protocol.endswith(".messages")
        else (OpenAIResponses if protocol.endswith(".responses") else OpenAIChatCompletions)
    )
    return adapter(api_key=config["api_key"], base_url=base_url)


def _call_payload(part: ToolCallPart) -> dict[str, Any]:
    return {
        "id": part.tool_call_id,
        "type": "function",
        "function": {"name": part.tool_name, "arguments": part.tool_args},
    }


def stored_message(message: Message, protocol: str) -> dict[str, Any]:
    """Keep the familiar tape view and one versioned, lossless native payload."""
    result: dict[str, Any] = {"role": message.role, "content": message.text}
    if message.tool_calls:
        result["tool_calls"] = [_call_payload(call) for call in message.tool_calls]
    if message.role == "tool":
        if len(message.parts) != 1 or not isinstance(message.parts[0], ToolResultPart):
            raise BubError(ErrorKind.INVALID_INPUT, "Expected one tool result per tape message.")
        part = message.parts[0]
        result.update(content=render_tool_result(part.result), tool_call_id=part.tool_call_id, name=part.tool_name)
    result["_republic"] = {"version": 1, "protocol": protocol, "message": message.model_dump(mode="json")}
    return result


def _read_message(raw: dict[str, Any], protocol: str) -> Message:
    if "_republic" in raw:
        native = raw["_republic"]
        if not isinstance(native, dict) or native.get("version") != 1 or native.get("protocol") != protocol:
            raise BubError(ErrorKind.INVALID_INPUT, "Native tape history belongs to another Republic protocol/version.")
        message = Message.model_validate(native["message"])
        if stored_message(message, protocol) != raw:
            raise BubError(
                ErrorKind.INVALID_INPUT, "Tape view differs from its native message; edit native data explicitly."
            )
        return message
    allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
    if raw.keys() - allowed:
        raise BubError(ErrorKind.INVALID_INPUT, "Legacy message has unsupported fields; no metadata may be discarded.")
    parts: list[Any] = []
    content = raw.get("content")
    if raw["role"] == "tool":
        if "tool_calls" in raw:
            raise BubError(ErrorKind.INVALID_INPUT, "Tool results cannot also contain tool calls.")
        if not raw.get("tool_call_id") or not raw.get("name"):
            raise BubError(ErrorKind.INVALID_INPUT, "Tool history requires its original call ID and name.")
        return Message(
            role="tool",
            parts=[
                ToolResultPart(
                    tool_call_id=raw["tool_call_id"],
                    tool_name=raw["name"],
                    result=content,
                )
            ],
        )
    if "tool_call_id" in raw or "name" in raw:
        raise BubError(ErrorKind.INVALID_INPUT, "Tool-result fields require a tool role.")
    parts.extend(_text_parts(content))
    parts.extend(_legacy_calls(raw.get("tool_calls")))
    return Message(role=raw["role"], parts=parts)


def _legacy_calls(calls: Any) -> list[ToolCallPart]:
    if calls is not None and not isinstance(calls, list):
        raise BubError(ErrorKind.INVALID_INPUT, "Expected a list of tool calls.")
    parts = []
    for call in calls or []:
        if not isinstance(call, dict) or set(call) != {"id", "type", "function"} or call["type"] != "function":
            raise BubError(ErrorKind.INVALID_INPUT, "Unsupported legacy tool call.")
        function = call["function"]
        if (
            not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
            or not call["id"]
            or not function["name"]
        ):
            raise BubError(ErrorKind.INVALID_INPUT, "Tool history requires its original ID, name and arguments.")
        parts.append(ToolCallPart(tool_call_id=call["id"], tool_name=function["name"], tool_args=function["arguments"]))
    return parts


def _text_parts(content: Any) -> list[TextPart]:
    parts: list[TextPart] = []
    if isinstance(content, str):
        parts.append(TextPart(text=content))
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "text":
                raise BubError(ErrorKind.INVALID_INPUT, "Republic integration currently accepts text input only.")
            parts.append(TextPart(text=part["text"]))
    elif content is not None:
        raise BubError(ErrorKind.INVALID_INPUT, "Unsupported message content.")
    return parts


def _messages(raw: list[dict[str, Any]], protocol: str) -> list[Message]:
    result = [_read_message(item, protocol) for item in raw]
    if not protocol.endswith(".messages"):
        # Chat/Responses have no is_error bit; encode the failure explicitly in
        # caller-owned result data. The durable native result still keeps the bit.
        for message in result:
            for part in message.parts:
                if isinstance(part, ToolResultPart) and part.is_error:
                    part.result = {"is_error": True, "result": part.result}
                    part.is_error = False
    return result


def _options(settings: AgentSettings, request: LlmCallRequest, protocol: str, effort: str | None) -> RequestOptions:
    raw = deepcopy(settings.completion_args)
    if "max_output_tokens" in raw:
        raise BubError(ErrorKind.CONFIG, "Set Bub max_tokens; max_output_tokens is managed by the runner.")
    raw["max_output_tokens"] = request.max_tokens
    if effort not in (None, "auto"):
        native = raw.setdefault("provider_options", {})
        key = "reasoning" if protocol.endswith(".responses") else "reasoning_effort"
        if protocol.endswith(".messages") or key in native:
            raise BubError(
                ErrorKind.CONFIG, "Reasoning effort is unsupported or conflicts with explicit native options."
            )
        native[key] = {"effort": effort} if key == "reasoning" else effort
    return RequestOptions.model_validate(raw)


def _accept_response(
    response: Response | None, protocol: str, output: ModelOutputAccumulator, state: StreamState
) -> None:
    if response is None:
        raise BubError(ErrorKind.PROVIDER, "Republic stream has no terminal response.")
    output.response = response
    output.native_protocol = protocol
    output.native_message = stored_message(response.message, protocol)
    output.native_calls = [_call_payload(call) for call in response.message.tool_calls]
    output.finish_reason = response.finish_reason
    if response.usage is not None:
        usage = response.usage
        state.usage = {
            **usage.model_dump(mode="json"),
            "total_tokens": usage.total_tokens,
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "prompt_tokens_details": {"cached_tokens": usage.cache_read_tokens},
        }
    if span := current_span():
        span.set(**{
            "gen_ai.response.id": response.response_id,
            "gen_ai.response.model": response.response_model,
            "gen_ai.response.finish_reasons": [response.finish_reason],
        })
    if response.finish_reason not in {"stop", "tool_call"} or (
        output.native_calls and response.finish_reason != "tool_call"
    ):
        output.failure = BubError(ErrorKind.PROVIDER, f"Incomplete model outcome: {response.finish_reason}")
        return
    for call in response.message.tool_calls:
        info = (call.provider_metadata or {}).get("openai")
        raw = info.get("raw_item") if isinstance(info, dict) else None
        status = raw.get("status") if isinstance(raw, dict) else None
        try:
            arguments = json.loads(call.tool_args)
        except ValueError:
            arguments = None
        if (
            not call.tool_call_id
            or not call.tool_name
            or not isinstance(arguments, dict)
            or (status is not None and status != "completed")
        ):
            output.failure = BubError(
                ErrorKind.INVALID_INPUT, "Model tool call is incomplete or has invalid object JSON."
            )
            return


async def completion_events(
    runner: ModelRunner,
    request: LlmCallRequest,
    tools: list[Tool],
    tape: Tape,
    state: StreamState,
    output: ModelOutputAccumulator,
) -> AsyncGenerator[StreamEvent, None]:
    """Bub's explicit fallback policy surrounds individual Republic operations."""
    first_error = None
    candidates = runner.settings.model_candidates(request.model)
    for index, candidate in enumerate(candidates):
        protocol = protocol_for(runner.settings, candidate)
        native_request = Request(
            model=candidate.model_id,
            messages=_messages(request.messages, protocol),
            tools=[
                ModelTool(name=tool.name, description=tool.description, parameters=tool.parameters) for tool in tools
            ],
            options=_options(runner.settings, request, protocol, tape.context.state.get("reasoning_effort")),
        )
        observed = False
        try:
            async with (
                runner.create_republic_provider(candidate) as provider,
                stream(provider, native_request) as response_stream,
            ):
                async for item in response_stream:
                    observed = True
                    if isinstance(item, events.TextDelta):
                        output.add_text(item.chunk)
                        yield StreamEvent("text", {"delta": item.chunk})
                    elif isinstance(item, events.ReasoningDelta):
                        yield StreamEvent("reasoning", {"delta": item.chunk})
                _accept_response(response_stream.response, protocol, output, state)
        except ProviderError as exc:
            event("bub.model.attempt_failed", model=candidate.name, error=repr(exc))
            if observed:
                raise
            if first_error is None:
                first_error = exc
            if index == len(candidates) - 1:
                raise first_error from None
        else:
            return
    raise BubError(ErrorKind.CONFIG, "No Republic model candidates configured.")


def tool_result_messages(protocol: str, calls: list[dict[str, Any]], execution: ToolExecution) -> list[dict[str, Any]]:
    return [
        stored_message(
            Message(
                role="tool",
                parts=[
                    ToolResultPart(
                        tool_call_id=call["id"],
                        tool_name=call["function"]["name"],
                        result=render_tool_result(result),
                        is_error=failed,
                    )
                ],
            ),
            protocol,
        )
        for call, result, failed in zip(calls, execution.tool_results, execution.tool_errors, strict=True)
    ]
