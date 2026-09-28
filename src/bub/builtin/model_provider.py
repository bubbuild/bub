"""Republic model boundary: native model data, with execution/lifecycle in Bub."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from republic import (
    FilePart,
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
from republic.providers.codex import OpenAICodex
from republic.providers.openai import OpenAIChatCompletions, OpenAIResponses

from bub.builtin import auth
from bub.builtin.context import render_tool_result
from bub.builtin.settings import DEFAULT_MAX_TOKENS, AgentSettings, ModelCandidate
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import LlmCallRequest
from bub.streaming import StreamEvent, StreamState
from bub.tape import Tape
from bub.tools import Tool, ToolExecution
from bub.tracing import current_span, event

if TYPE_CHECKING:
    from bub.builtin.model_runner import ModelRunner

Provider = OpenAIChatCompletions | OpenAIResponses | AnthropicMessages | OpenAICodex
_DEFAULTS = {"openai": "chat", "openrouter": "chat", "anthropic": "messages"}


def protocol_for(settings: AgentSettings, candidate: ModelCandidate) -> str:
    provider = candidate.provider
    if settings.republic_protocols.keys() - _DEFAULTS.keys():
        raise BubError(ErrorKind.CONFIG, "Unsupported Republic provider configuration.")
    protocol = settings.republic_protocols.get(provider, _DEFAULTS.get(provider))
    config = settings.model_client_kwargs(provider)
    if provider == "openai" and provider not in settings.republic_protocols and not config["api_base"]:
        key = config["api_key"]
        if (key and auth.codex_account_id(key)) or (not key and auth.load_codex_tokens(settings.codex_home)):
            protocol = "codex"
    if (provider, protocol) not in {
        ("openai", "chat"),
        ("openai", "responses"),
        ("openai", "codex"),
        ("openrouter", "chat"),
        ("anthropic", "messages"),
    }:
        raise BubError(ErrorKind.CONFIG, f"Republic does not support {provider}/{protocol} in Bub.")
    return f"{provider}.{protocol}"


async def create_provider(settings: AgentSettings, candidate: ModelCandidate) -> Provider:
    protocol = protocol_for(settings, candidate)
    config = settings.model_client_kwargs(candidate.provider)
    if protocol == "openai.codex":
        key = config["api_key"]
        credentials = key if key else await auth.prepare_codex_tokens(settings.codex_home)
        return OpenAICodex(
            credentials,
            account_id=auth.codex_account_id(key) if key else None,
            base_url=config["api_base"],
            max_retries=0,
            headers={"originator": "bub", "OpenAI-Beta": "responses=experimental"},
        )
    if not config["api_key"]:
        raise BubError(ErrorKind.CONFIG, "Set a Bub API key, or use bub login openai for Codex.")
    base_url = config["api_base"]
    if candidate.provider == "openrouter" and base_url is None:
        base_url = "https://openrouter.ai/api/v1"
    adapter = (
        AnthropicMessages
        if protocol.endswith(".messages")
        else (OpenAIResponses if protocol.endswith(".responses") else OpenAIChatCompletions)
    )
    return adapter(api_key=config["api_key"], base_url=base_url, max_retries=0)


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
    parts.extend(_content_parts(content))
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


def _media_part(raw: dict[str, Any]) -> FilePart:
    kind = raw.get("type")
    if kind in {"image_url", "video_url"}:
        data = raw.get(kind)
        allowed = {"url", "detail"} if kind == "image_url" else {"url"}
        if (
            raw.keys() - {"type", kind, "processing"}
            or not isinstance(data, dict)
            or data.keys() - allowed
            or not isinstance(data.get("url"), str)
            or (kind == "image_url" and "processing" in raw)
        ):
            raise BubError(ErrorKind.INVALID_INPUT, "Unsupported media URL content or metadata.")
        url = data["url"]
        media_type = url[5:].partition(";")[0] if url.startswith("data:") else f"{kind.removesuffix('_url')}/*"
        if not media_type.startswith(kind.removesuffix("_url") + "/"):
            raise BubError(ErrorKind.INVALID_INPUT, "Media data URL does not match its content type.")
        metadata = {key: value for key, value in data.items() if key != "url"}
        if "processing" in raw:
            metadata["processing"] = raw["processing"]
        return FilePart(data=url, media_type=media_type, provider_metadata={"openai": metadata} if metadata else None)
    if kind == "input_audio":
        audio = raw.get("input_audio")
        if (
            set(raw) != {"type", "input_audio"}
            or not isinstance(audio, dict)
            or set(audio) != {"data", "format"}
            or any(not isinstance(audio.get(key), str) or not audio[key] for key in ("data", "format"))
        ):
            raise BubError(ErrorKind.INVALID_INPUT, "Expected base64 audio data and its format.")
        return FilePart(data=audio["data"], media_type=f"audio/{audio['format']}", encoding="base64")
    raise BubError(ErrorKind.INVALID_INPUT, "Unsupported message content part.")


def _content_parts(content: Any) -> list[TextPart | FilePart]:
    if isinstance(content, str):
        return [TextPart(text=content)]
    if content is None:
        return []
    if not isinstance(content, list):
        raise BubError(ErrorKind.INVALID_INPUT, "Unsupported message content.")
    parts: list[TextPart | FilePart] = []
    for part in content:
        if not isinstance(part, dict):
            raise BubError(ErrorKind.INVALID_INPUT, "Expected a content part object.")
        if part.get("type") == "text":
            if set(part) != {"type", "text"}:
                raise BubError(ErrorKind.INVALID_INPUT, "Unsupported text part metadata.")
            parts.append(TextPart(text=part["text"]))
        else:
            parts.append(_media_part(part))
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
    if not protocol.endswith(".codex") and request.max_tokens is None:
        raw["max_output_tokens"] = DEFAULT_MAX_TOKENS
    if protocol.endswith(".messages"):
        raw.setdefault("provider_options", {}).setdefault("cache_control", {"type": "ephemeral"})
    if effort not in (None, "auto"):
        native = raw.setdefault("provider_options", {})
        key = "reasoning" if protocol.endswith((".responses", ".codex")) else "reasoning_effort"
        if protocol.endswith(".messages") or key in native:
            raise BubError(
                ErrorKind.CONFIG, "Reasoning effort is unsupported or conflicts with explicit native options."
            )
        native[key] = {"effort": effort} if key == "reasoning" else effort
    return RequestOptions.model_validate(raw)


def _accept_response(response: Response | None, protocol: str, output: ModelOutput, state: StreamState) -> None:
    if response is None:
        raise BubError(ErrorKind.PROVIDER, "Republic stream has no terminal response.")
    output.response = response
    output.protocol = protocol
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
        output.serialized_tool_calls and response.finish_reason != "tool_call"
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
    output: ModelOutput,
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
        if span := current_span():
            span.rename(f"chat {candidate.model_id}")
            span.set(**{"gen_ai.provider.name": candidate.provider, "gen_ai.request.model": candidate.model_id})
        observed = False
        try:
            async with (
                await runner.create_provider(candidate) as provider,
                stream(provider, native_request) as response_stream,
            ):
                async for item in response_stream:
                    observed = True
                    if isinstance(item, events.TextDelta):
                        output.text += item.chunk
                        yield StreamEvent("text", {"delta": item.chunk})
                    elif isinstance(item, events.ReasoningDelta):
                        yield StreamEvent("reasoning", {"delta": item.chunk})
                _accept_response(response_stream.response, protocol, output, state)
        except ProviderError as exc:
            event("bub.model.attempt_failed", model=candidate.name, error=repr(exc))
            if observed:
                raise
            first_error = first_error or exc
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


@dataclass
class ModelOutput:
    """Caller state for partial display, a native response and execution gating."""

    text: str = ""
    response: Response | None = None
    protocol: str | None = None
    failure: BubError | None = None

    @property
    def finish_reason(self) -> str | None:
        return self.response.finish_reason if self.response else None

    @property
    def native_message(self) -> dict[str, Any] | None:
        return stored_message(self.response.message, self.protocol) if self.response and self.protocol else None

    @property
    def serialized_tool_calls(self) -> list[dict[str, Any]]:
        return [_call_payload(call) for call in self.response.message.tool_calls] if self.response else []

    def invocations(self, tools: dict[str, Tool]) -> list[tuple[Tool, dict[str, Any]]]:
        return [tool_invocation(call, tools) for call in self.response.message.tool_calls] if self.response else []

    def result_messages(self, execution: ToolExecution) -> list[dict[str, Any]]:
        if self.protocol is None:
            raise BubError(ErrorKind.PROVIDER, "No provider response for tool execution.")
        return tool_result_messages(self.protocol, self.serialized_tool_calls, execution)


def tool_invocation(call: ToolCallPart, tools: dict[str, Tool]) -> tuple[Tool, dict[str, Any]]:
    """An unknown tool still flows through Bub hooks and produces a tool error."""
    try:
        arguments = json.loads(call.tool_args)
    except ValueError:
        arguments = None
    if not isinstance(arguments, dict):
        raise BubError(ErrorKind.INVALID_INPUT, "Expected JSON object tool arguments.")
    tool = tools.get(call.tool_name)
    if tool is None:

        def missing(**_: Any) -> None:
            raise BubError(ErrorKind.TOOL, f"Unknown tool name: {call.tool_name}.")

        tool = Tool(name=call.tool_name, handler=missing)
    return tool, arguments
