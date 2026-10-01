"""OpenAI Codex OAuth provider for any-llm Responses calls."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast, override

from any_llm.exceptions import MissingApiKeyError
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import ChatCompletion, ChatCompletionChunk, CompletionParams
from any_llm.types.model import Model
from any_llm.types.responses import Response, ResponsesParams

from bub.builtin.auth import (
    extract_openai_codex_account_id,
    load_openai_codex_oauth_tokens,
    openai_codex_oauth_resolver,
)

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_CODEX_ORIGINATOR = "bub"
DEFAULT_CODEX_INCLUDE = ["reasoning.encrypted_content"]
DEFAULT_CODEX_INSTRUCTIONS = "You are Codex."
DEFAULT_CODEX_TEXT_CONFIG = {"verbosity": "medium"}


class OpenAICodexTransportError(RuntimeError):
    def __init__(self, status_code: int | None, message: str, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OpenaiCodexProvider(BaseOpenAIProvider):
    """OpenAI-compatible Responses provider backed by Codex OAuth credentials."""

    API_BASE = DEFAULT_CODEX_BASE_URL
    ENV_API_KEY_NAME = "OPENAI_CODEX_API_KEY"
    ENV_API_BASE_NAME = "OPENAI_CODEX_API_BASE"
    PROVIDER_NAME = "openaicodex"
    PROVIDER_DOCUMENTATION_URL = "https://platform.openai.com/docs/codex"

    SUPPORTS_COMPLETION_STREAMING = True
    SUPPORTS_COMPLETION = True
    SUPPORTS_COMPLETION_REASONING = True
    SUPPORTS_RESPONSES = True
    SUPPORTS_LIST_MODELS = False
    SUPPORTS_BATCH = False
    SUPPORTS_IMAGE_GENERATION = False
    SUPPORTS_AUDIO_TRANSCRIPTION = False
    SUPPORTS_AUDIO_SPEECH = False
    SUPPORTS_EMBEDDING = False
    SUPPORTS_MODERATION = False

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        codex_home: str | None = None,
        default_instructions: str = DEFAULT_CODEX_INSTRUCTIONS,
        default_include: Sequence[str] = tuple(DEFAULT_CODEX_INCLUDE),
        default_text: dict[str, Any] | None = None,
        originator: str = DEFAULT_CODEX_ORIGINATOR,
        store: bool = False,
        **kwargs: Any,
    ) -> None:
        self._codex_home = codex_home
        self._default_instructions = default_instructions
        self._default_include = list(default_include)
        self._default_text = dict(default_text or DEFAULT_CODEX_TEXT_CONFIG)
        self._originator = originator
        self._store = store
        super().__init__(api_key=api_key, api_base=api_base, **kwargs)

    @override
    def _verify_and_set_api_key(self, api_key: str | None = None) -> str | None:
        if api_key:
            return api_key
        resolved = openai_codex_oauth_resolver(self._codex_home)("openai")
        if not resolved:
            raise MissingApiKeyError(self.PROVIDER_NAME, self.ENV_API_KEY_NAME)
        return resolved

    @override
    def _init_client(self, api_key: str | None = None, api_base: str | None = None, **kwargs: Any) -> None:
        default_headers = dict(kwargs.pop("default_headers", {}))
        default_headers.update(build_openai_codex_default_headers(api_key or "", originator=self._originator))
        super()._init_client(
            api_key=api_key,
            api_base=resolve_openai_codex_api_base(api_base),
            default_headers=default_headers,
            **kwargs,
        )

    @staticmethod
    @override
    def _convert_list_models_response(response: Any) -> Sequence[Model]:
        raise NotImplementedError("OpenAI Codex OAuth provider does not support listing models.")

    @override
    async def _acompletion(
        self,
        params: CompletionParams,
        **kwargs: Any,
    ) -> ChatCompletion | AsyncIterator[ChatCompletionChunk]:
        responses_params = self._completion_params_to_responses_params(params)
        if params.stream:
            response = await self._aresponses(responses_params.model_copy(update={"stream": True}))
            if not hasattr(response, "__aiter__"):
                raise OpenAICodexTransportError(None, "OpenAI Codex Responses API returned a non-streaming result.")
            return self._response_stream_to_completion_chunks(
                cast("AsyncIterator[Any]", response),
                model=params.model_id,
            )

        response = await self._aresponses(responses_params.model_copy(update={"stream": False}))
        return self._response_to_completion(cast("Response", response), model=params.model_id)

    @override
    async def _aresponses(self, params: ResponsesParams, **kwargs: Any) -> Any:
        return await super()._aresponses(self._with_codex_response_defaults(params), **kwargs)

    def _completion_params_to_responses_params(self, params: CompletionParams) -> ResponsesParams:
        reasoning = None
        if params.reasoning_effort not in {None, "auto"}:
            reasoning = {"effort": params.reasoning_effort}

        return ResponsesParams(
            model=params.model_id,
            input=cast("Any", _completion_messages_to_responses_input(params.messages)),
            tools=self._completion_tools_to_response_tools(cast("Sequence[Any] | None", params.tools)),
            tool_choice=self._completion_tool_choice_to_response_tool_choice(params.tool_choice),
            response_format=params.response_format,
            stream=params.stream,
            parallel_tool_calls=params.parallel_tool_calls,
            reasoning=reasoning,
        )

    @staticmethod
    def _completion_tools_to_response_tools(tools: Sequence[Any] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        response_tools: list[dict[str, Any]] = []
        for tool in tools:
            payload = tool.model_dump(exclude_none=True) if hasattr(tool, "model_dump") else dict(tool)
            function = payload.get("function")
            if payload.get("type") == "function" and isinstance(function, dict):
                response_tools.append({
                    "type": "function",
                    "name": function.get("name", ""),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                })
            else:
                response_tools.append(payload)
        return response_tools

    @staticmethod
    def _completion_tool_choice_to_response_tool_choice(tool_choice: str | dict[str, Any] | None) -> Any:
        if not isinstance(tool_choice, dict):
            return tool_choice
        function = tool_choice.get("function")
        if tool_choice.get("type") == "function" and isinstance(function, dict):
            return {"type": "function", "name": function.get("name", "")}
        return tool_choice

    async def _response_stream_to_completion_chunks(
        self,
        events: AsyncIterator[Any],
        *,
        model: str,
    ) -> AsyncIterator[ChatCompletionChunk]:
        mapper = CodexCompletionChunkMapper(model=model)
        async for event in events:
            for chunk in mapper.map_event(event):
                yield chunk

    @staticmethod
    def _response_to_completion(response: Response, *, model: str) -> ChatCompletion:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) == "message":
                for content in getattr(item, "content", []) or []:
                    text = getattr(content, "text", None)
                    if isinstance(text, str):
                        text_parts.append(text)
            if getattr(item, "type", None) in {"function_call", "custom_tool_call"}:
                tool_calls.append({
                    "id": getattr(item, "call_id", None) or getattr(item, "id", None) or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": getattr(item, "name", "") or "",
                        "arguments": _tool_item_arguments(item) or "{}",
                    },
                })

        usage = _completion_usage_from_response_usage(getattr(response, "usage", None))
        return ChatCompletion.model_validate({
            "id": getattr(response, "id", None) or "chatcmpl_codex",
            "object": "chat.completion",
            "created": int(getattr(response, "created_at", None) or time.time()),
            "model": getattr(response, "model", None) or model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                    "message": {
                        "role": "assistant",
                        "content": "".join(text_parts) or None,
                        "tool_calls": tool_calls or None,
                    },
                }
            ],
            "usage": usage,
        })

    def _with_codex_response_defaults(self, params: ResponsesParams) -> ResponsesParams:
        update: dict[str, Any] = {}
        if params.store is None:
            update["store"] = self._store
        if params.instructions is None:
            update["instructions"] = self._default_instructions
        if params.include is None:
            update["include"] = list(self._default_include)
        if isinstance(params.text, dict):
            update["text"] = {**self._default_text, **params.text}
        elif params.text is None:
            update["text"] = dict(self._default_text)
        return params.model_copy(update=update)


@dataclass
class _CodexToolState:
    started: bool = False
    arguments_seen: bool = False


class CodexCompletionChunkMapper:
    def __init__(self, *, model: str) -> None:
        self.model = model
        self.created = int(time.time())
        self._tool_states: dict[int, _CodexToolState] = {}
        self._tool_indexes_by_identifier: dict[str, int] = {}

    def map_event(self, event: Any) -> list[ChatCompletionChunk]:
        event_type = getattr(event, "type", None)
        if event_type == "response.output_text.delta":
            return [self._delta_chunk({"content": _string_attr(event, "delta")})]
        if event_type in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
            return [self._delta_chunk({"reasoning": _string_attr(event, "delta")})]
        if event_type == "response.output_item.added":
            return self._tool_item_chunks(event)
        if event_type in {"response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}:
            return self._tool_arguments_delta_chunks(event)
        if event_type == "response.function_call_arguments.done":
            return self._tool_arguments_done_chunks(event)
        if event_type == "response.output_item.done":
            return self._tool_item_chunks(event, done=True)
        if event_type == "response.completed":
            response = getattr(event, "response", None)
            return [self._terminal_chunk(response=response)]
        if event_type in {"response.failed", "response.incomplete"}:
            raise OpenAICodexTransportError(None, f"OpenAI Codex response ended with {event_type}")
        if event_type == "error":
            message = getattr(event, "message", None) or "OpenAI Codex response stream error"
            raise OpenAICodexTransportError(None, str(message))
        return []

    def _tool_item_chunks(self, event: Any, *, done: bool = False) -> list[ChatCompletionChunk]:
        output_index = getattr(event, "output_index", None)
        item = getattr(event, "item", None)
        if getattr(item, "type", None) not in {"function_call", "custom_tool_call"} or not isinstance(
            output_index, int
        ):
            return []

        self._remember_tool_identifiers(item, output_index)
        state = self._tool_states.setdefault(output_index, _CodexToolState())
        if done and state.started and state.arguments_seen:
            return []

        tool_delta: dict[str, Any] = {
            "index": output_index,
            "id": getattr(item, "call_id", None) or getattr(item, "id", None) or f"call_{output_index}",
            "type": "function",
            "function": {
                "name": getattr(item, "name", None) or "",
                "arguments": "" if state.arguments_seen else _tool_item_arguments(item),
            },
        }
        state.started = True
        if tool_delta["function"]["arguments"]:
            state.arguments_seen = True
        return [self._delta_chunk({"tool_calls": [tool_delta]})]

    def _tool_arguments_delta_chunks(self, event: Any) -> list[ChatCompletionChunk]:
        output_index = self._tool_index_for_event(event)
        if output_index is None:
            return []
        delta = _string_attr(event, "delta")
        if not delta:
            return []
        state = self._tool_states.setdefault(output_index, _CodexToolState())
        state.arguments_seen = True
        return [self._delta_chunk({"tool_calls": [{"index": output_index, "function": {"arguments": delta}}]})]

    def _tool_arguments_done_chunks(self, event: Any) -> list[ChatCompletionChunk]:
        output_index = self._tool_index_for_event(event)
        if output_index is None:
            return []
        state = self._tool_states.setdefault(output_index, _CodexToolState())
        if state.arguments_seen:
            return []
        arguments = _string_attr(event, "arguments")
        if not arguments:
            return []
        state.arguments_seen = True
        name = _string_attr(event, "name")
        function: dict[str, str] = {"arguments": arguments}
        if name:
            function["name"] = name
        return [self._delta_chunk({"tool_calls": [{"index": output_index, "function": function}]})]

    def _tool_index_for_event(self, event: Any) -> int | None:
        output_index = getattr(event, "output_index", None)
        if isinstance(output_index, int):
            return output_index
        for attr in ("item_id", "call_id"):
            identifier = getattr(event, attr, None)
            if isinstance(identifier, str):
                known_index = self._tool_indexes_by_identifier.get(identifier)
                if known_index is not None:
                    return known_index
        return None

    def _remember_tool_identifiers(self, item: Any, output_index: int) -> None:
        for attr in ("id", "call_id"):
            identifier = getattr(item, attr, None)
            if isinstance(identifier, str) and identifier:
                self._tool_indexes_by_identifier[identifier] = output_index

    def _delta_chunk(self, delta: dict[str, Any]) -> ChatCompletionChunk:
        return ChatCompletionChunk.model_validate({
            "id": "chatcmpl_codex",
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        })

    def _terminal_chunk(self, *, response: Any) -> ChatCompletionChunk:
        usage = _completion_usage_from_response_usage(getattr(response, "usage", None))
        finish_reason = "tool_calls" if self._tool_states else "stop"
        return ChatCompletionChunk.model_validate({
            "id": getattr(response, "id", None) or "chatcmpl_codex",
            "object": "chat.completion.chunk",
            "created": int(getattr(response, "created_at", None) or self.created),
            "model": getattr(response, "model", None) or self.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage,
        })


def _completion_usage_from_response_usage(usage: Any) -> dict[str, Any]:
    payload = usage.model_dump(exclude_none=True) if hasattr(usage, "model_dump") else usage
    if not isinstance(payload, dict):
        payload = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
            "input_tokens_details": getattr(usage, "input_tokens_details", None),
        }
    prompt_tokens = int(payload.get("input_tokens") or payload.get("prompt_tokens") or 0)
    completion_tokens = int(payload.get("output_tokens") or payload.get("completion_tokens") or 0)
    total_tokens = int(payload.get("total_tokens") or prompt_tokens + completion_tokens)
    completion_usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    input_details = _mapping_from_value(payload.get("input_tokens_details"))
    cached_tokens = input_details.get("cached_tokens")
    if isinstance(cached_tokens, int) and not isinstance(cached_tokens, bool):
        completion_usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return completion_usage


def _string_attr(obj: Any, name: str) -> str:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else ""


def _tool_item_arguments(item: Any) -> str:
    return _string_attr(item, "arguments") or _string_attr(item, "input")


def _completion_messages_to_responses_input(messages: Sequence[Any]) -> list[dict[str, Any]]:
    response_input: list[dict[str, Any]] = []
    for message in messages:
        payload = _mapping_from_value(message)
        if not payload:
            continue

        role = payload.get("role")
        if role == "tool":
            tool_result = _completion_tool_result_to_response_item(payload)
            if tool_result is not None:
                response_input.append(tool_result)
            continue

        tool_calls = payload.get("tool_calls")
        if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, str):
            content = payload.get("content")
            if content:
                response_input.append({"role": "assistant", "content": content})
            response_input.extend(_completion_tool_calls_to_response_items(tool_calls))
            continue

        if isinstance(role, str):
            response_input.append({"role": role, "content": payload.get("content") or ""})
    return response_input


def _completion_tool_calls_to_response_items(tool_calls: Sequence[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        payload = _mapping_from_value(tool_call)
        function = _mapping_from_value(payload.get("function"))
        call_id = payload.get("id")
        name = function.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            continue
        arguments = function.get("arguments")
        items.append({
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else "{}",
            "status": "completed",
        })
    return items


def _completion_tool_result_to_response_item(message: Mapping[str, Any]) -> dict[str, Any] | None:
    call_id = message.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    content = message.get("content")
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": content if isinstance(content, str) else "",
    }


def _mapping_from_value(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def should_use_openai_codex_provider(
    provider: str, model_id: str, *, api_key: str | None, api_base: str | None
) -> bool:
    if provider != "openai" or api_base:
        return False
    if api_key:
        return extract_openai_codex_account_id(api_key) is not None
    return load_openai_codex_oauth_tokens() is not None


def resolve_openai_codex_api_base(api_base: str | None) -> str:
    raw = (api_base or DEFAULT_CODEX_BASE_URL).rstrip("/")
    if raw.endswith("/responses"):
        raw = raw[: -len("/responses")]
    if raw.endswith("/codex"):
        return raw
    return f"{raw}/codex"


def build_openai_codex_default_headers(api_key: str, *, originator: str = DEFAULT_CODEX_ORIGINATOR) -> dict[str, str]:
    account_id = extract_openai_codex_account_id(api_key)
    if account_id is None:
        raise OpenAICodexTransportError(None, "OpenAI Codex OAuth token is missing chatgpt_account_id")
    return {
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": originator,
    }
