"""OpenAI Codex OAuth provider for any-llm."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast, override

from any_llm.exceptions import MissingApiKeyError
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import ChatCompletion, CompletionParams
from any_llm.types.model import Model
from any_llm.types.responses import Response, ResponsesParams
from openai import AsyncStream
from openai.types.responses import ResponseStreamEvent

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
    """any-llm provider backed by OpenAI Codex OAuth credentials."""

    API_BASE = DEFAULT_CODEX_BASE_URL
    ENV_API_KEY_NAME = "OPENAI_CODEX_API_KEY"
    ENV_API_BASE_NAME = "OPENAI_CODEX_API_BASE"
    PROVIDER_NAME = "openaicodex"
    PROVIDER_DOCUMENTATION_URL = "https://platform.openai.com/docs/codex"

    SUPPORTS_COMPLETION_STREAMING = False
    SUPPORTS_COMPLETION = True
    SUPPORTS_COMPLETION_REASONING = True
    SUPPORTS_RESPONSES = True
    SUPPORTS_LIST_MODELS = False
    SUPPORTS_BATCH = False
    SUPPORTS_IMAGE_GENERATION = False
    SUPPORTS_AUDIO_TRANSCRIPTION = False
    SUPPORTS_AUDIO_SPEECH = False

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
    async def _acompletion(self, params: CompletionParams, **kwargs: Any) -> ChatCompletion:
        """Implement Chat Completions via Codex Responses while returning any-llm's completion type."""

        responses_params = self._completion_params_to_responses_params(params, **kwargs)
        response = await self._aresponses(responses_params)
        if isinstance(response, AsyncStream):
            msg = "OpenAI Codex completion streaming is disabled for Bub's any-llm provider."
            raise OpenAICodexTransportError(None, msg)
        return self._response_to_completion(response, model=params.model_id)

    @override
    async def _aresponses(self, params: ResponsesParams, **kwargs: Any) -> Response | AsyncStream[ResponseStreamEvent]:
        payload = self._build_responses_payload(params, **kwargs)
        response = await self.client.responses.create(**payload)
        if params.stream:
            return cast("AsyncStream[ResponseStreamEvent]", response)
        if isinstance(response, AsyncStream):
            return self._collect_response_events(await self._collect_events(response))
        return cast("Response", response)

    def _completion_params_to_responses_params(self, params: CompletionParams, **kwargs: Any) -> ResponsesParams:
        completion_kwargs = self._convert_completion_params(params, **kwargs)
        tools = completion_kwargs.pop("tools", None)
        tool_choice = completion_kwargs.pop("tool_choice", None)
        response_format = completion_kwargs.pop("response_format", None)
        parallel_tool_calls = completion_kwargs.pop("parallel_tool_calls", None)
        max_output_tokens = completion_kwargs.pop("max_completion_tokens", None)
        completion_kwargs.pop("stream", None)
        completion_kwargs.pop("n", None)
        completion_kwargs.pop("stop", None)
        completion_kwargs.pop("logprobs", None)
        completion_kwargs.pop("top_logprobs", None)
        completion_kwargs.pop("logit_bias", None)
        completion_kwargs.pop("stream_options", None)

        reasoning_effort = completion_kwargs.pop("reasoning_effort", None)
        reasoning = completion_kwargs.pop("reasoning", None)
        if reasoning is None and reasoning_effort not in {None, "auto"}:
            reasoning = {"effort": reasoning_effort}

        return ResponsesParams(
            model=params.model_id,
            input=cast("Any", params.messages),
            tools=self._convert_tools_for_responses(cast("list[dict[str, Any] | Any] | None", tools or params.tools)),
            tool_choice=self._convert_tool_choice_for_responses(
                cast("str | dict[str, Any] | None", tool_choice or params.tool_choice)
            ),
            max_output_tokens=max_output_tokens,
            parallel_tool_calls=parallel_tool_calls or params.parallel_tool_calls,
            response_format=response_format or params.response_format,
            reasoning=reasoning,
            **completion_kwargs,
        )

    def _build_responses_payload(self, params: ResponsesParams, **kwargs: Any) -> dict[str, Any]:
        payload = params.model_dump(exclude_none=True, exclude={"response_format"})
        payload["stream"] = True
        payload.pop("max_output_tokens", None)
        payload["store"] = payload.get("store", self._store)
        payload["instructions"] = payload.get("instructions") or self._default_instructions
        payload["include"] = payload.get("include") or list(self._default_include)

        text = payload.get("text")
        if isinstance(text, dict):
            payload["text"] = {**self._default_text, **text}
        elif text is None:
            payload["text"] = dict(self._default_text)

        payload.update(kwargs)
        return payload

    @staticmethod
    async def _collect_events(response: AsyncIterator[Any]) -> list[Any]:
        events: list[Any] = []
        async for event in response:
            events.append(event)
        return events

    @staticmethod
    def _collect_response_events(events: list[Any]) -> Response:
        text_parts: list[str] = []
        tool_calls: dict[str, dict[str, Any]] = {}
        usage: dict[str, Any] | Any | None = None
        completed_response: Response | None = None

        for event in events:
            event_type = getattr(event, "type", None)
            if event_type == "response.output_text.delta":
                if isinstance(delta := getattr(event, "delta", None), str):
                    text_parts.append(delta)
                continue
            if event_type == "response.output_item.done":
                OpenaiCodexProvider._record_stream_tool_call(
                    tool_calls,
                    OpenaiCodexProvider._function_call_from_output_item(event),
                )
                continue
            if event_type == "response.function_call_arguments.done":
                OpenaiCodexProvider._record_stream_tool_call(
                    tool_calls,
                    OpenaiCodexProvider._function_call_from_arguments_done(event),
                )
                continue
            if event_type == "response.completed":
                completed = getattr(event, "response", None)
                completed_response = completed if isinstance(completed, Response) else None
                usage = getattr(completed, "usage", None) or usage
                continue
            usage = getattr(event, "usage", None) or usage

        if completed_response is not None and completed_response.output:
            return completed_response

        return OpenaiCodexProvider._build_response_from_stream(
            completed_response=completed_response,
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
        )

    @staticmethod
    def _build_response_from_stream(
        *,
        completed_response: Response | None,
        text: str,
        tool_calls: dict[str, dict[str, Any]],
        usage: dict[str, Any] | Any | None,
    ) -> Response:
        output: list[dict[str, Any]] = []
        if text:
            output.append({
                "id": "msg_codex",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            })
        output.extend(
            {
                "id": call.get("id"),
                "type": "function_call",
                "call_id": call["call_id"],
                "name": call.get("name"),
                "arguments": call.get("arguments", ""),
                "status": "completed",
            }
            for call in tool_calls.values()
        )

        payload = {
            "id": getattr(completed_response, "id", None) or "resp_codex",
            "object": getattr(completed_response, "object", None) or "response",
            "created_at": getattr(completed_response, "created_at", None) or time.time(),
            "status": getattr(completed_response, "status", None) or "completed",
            "error": getattr(completed_response, "error", None),
            "incomplete_details": getattr(completed_response, "incomplete_details", None),
            "instructions": getattr(completed_response, "instructions", None),
            "metadata": getattr(completed_response, "metadata", None) or {},
            "model": getattr(completed_response, "model", None) or "gpt-5.5",
            "output": output,
            "parallel_tool_calls": getattr(completed_response, "parallel_tool_calls", None) or False,
            "temperature": getattr(completed_response, "temperature", None),
            "tool_choice": getattr(completed_response, "tool_choice", None) or "auto",
            "tools": getattr(completed_response, "tools", None) or [],
            "top_p": getattr(completed_response, "top_p", None),
            "truncation": getattr(completed_response, "truncation", None) or "disabled",
            "usage": OpenaiCodexProvider._response_usage_payload(getattr(completed_response, "usage", None) or usage),
            "store": getattr(completed_response, "store", None) or False,
        }
        return Response.model_validate(payload)

    @staticmethod
    def _response_usage_payload(usage: dict[str, Any] | Any | None) -> dict[str, Any]:
        model_dump = getattr(usage, "model_dump", None)
        if callable(model_dump):
            payload = model_dump()
        elif isinstance(usage, dict):
            payload = dict(usage)
        else:
            payload = {}
        input_tokens = payload.get("input_tokens")
        output_tokens = payload.get("output_tokens")
        total_tokens = payload.get("total_tokens")
        if not isinstance(input_tokens, int):
            input_tokens = 0
        if not isinstance(output_tokens, int):
            output_tokens = 0
        if not isinstance(total_tokens, int):
            total_tokens = input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "input_tokens_details": payload.get("input_tokens_details") or {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": payload.get("output_tokens_details") or {"reasoning_tokens": 0},
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _record_stream_tool_call(tool_calls: dict[str, dict[str, Any]], tool_call: dict[str, Any] | None) -> None:
        if tool_call is None:
            return
        tool_calls[tool_call["call_id"]] = tool_call

    @staticmethod
    def _function_call_from_output_item(event: Any) -> dict[str, Any] | None:
        item = getattr(event, "item", None)
        if getattr(item, "type", None) != "function_call":
            return None
        call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
        if not isinstance(call_id, str) or not call_id:
            return None
        return {
            "call_id": call_id,
            "id": getattr(item, "id", None),
            "name": getattr(item, "name", None),
            "arguments": getattr(item, "arguments", "") or "",
        }

    @staticmethod
    def _function_call_from_arguments_done(event: Any) -> dict[str, Any] | None:
        call_id = getattr(event, "call_id", None) or getattr(event, "item_id", None)
        if not isinstance(call_id, str) or not call_id:
            return None
        return {
            "call_id": call_id,
            "id": getattr(event, "item_id", None),
            "name": getattr(event, "name", None),
            "arguments": getattr(event, "arguments", "") or "",
        }

    def _response_to_completion(self, response: Response, *, model: str) -> ChatCompletion:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self._response_output_text(response) or None,
        }
        if tool_calls := self._response_tool_calls(response):
            message["tool_calls"] = tool_calls
        if reasoning := self._response_reasoning(response):
            message["reasoning"] = reasoning

        payload = {
            "id": getattr(response, "id", None) or "chatcmpl_codex",
            "object": "chat.completion",
            "created": int(getattr(response, "created_at", None) or time.time()),
            "model": getattr(response, "model", None) or model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": self._completion_finish_reason(response),
                    "message": message,
                }
            ],
            "usage": self._completion_usage(response),
        }
        return self._convert_completion_response(payload)

    @staticmethod
    def _completion_finish_reason(response: Response) -> str:
        if OpenaiCodexProvider._response_tool_calls(response):
            return "tool_calls"
        status = getattr(response, "status", None)
        if status in {"incomplete", "failed", "cancelled"}:
            return "length"
        return "stop"

    @staticmethod
    def _response_output_text(response: Response) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str):
            return output_text

        parts: list[str] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) == "output_text":
                    text = getattr(content, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
        return "".join(parts)

    @staticmethod
    def _response_reasoning(response: Response) -> str | None:
        parts: list[str] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "reasoning":
                continue
            summary = getattr(item, "summary", None)
            if isinstance(summary, str):
                parts.append(summary)
            elif isinstance(summary, list):
                parts.extend(str(part) for part in summary if part)
        return "\n".join(parts) or None

    @staticmethod
    def _response_tool_calls(response: Response) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "function_call":
                continue
            name = getattr(item, "name", None)
            if not isinstance(name, str) or not name:
                continue
            calls.append({
                "id": getattr(item, "call_id", None) or getattr(item, "id", None) or f"call_{len(calls)}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": getattr(item, "arguments", None) or "{}",
                },
            })
        return calls

    @staticmethod
    def _completion_usage(response: Response) -> dict[str, Any] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            payload = usage.model_dump()
        elif isinstance(usage, dict):
            payload = dict(usage)
        else:
            payload = {
                key: value
                for key in ("input_tokens", "output_tokens", "total_tokens")
                if (value := getattr(usage, key, None)) is not None
            }
        prompt_tokens = int(payload.get("input_tokens") or 0)
        completion_tokens = int(payload.get("output_tokens") or 0)
        total_tokens = int(payload.get("total_tokens") or prompt_tokens + completion_tokens)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _convert_tools_for_responses(tools: list[dict[str, Any] | Any] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None

        converted: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                converted.append(cast("dict[str, Any]", tool))
                continue
            function = tool.get("function")
            if not isinstance(function, dict):
                converted.append(dict(tool))
                continue
            response_tool = {
                "type": tool.get("type", "function"),
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {}),
            }
            if "strict" in function:
                response_tool["strict"] = function["strict"]
            converted.append(response_tool)
        return converted

    @staticmethod
    def _convert_tool_choice_for_responses(tool_choice: str | dict[str, Any] | None) -> str | dict[str, Any] | None:
        if not isinstance(tool_choice, dict):
            return tool_choice
        function = tool_choice.get("function")
        if not isinstance(function, dict):
            return tool_choice
        function_name = function.get("name")
        if not isinstance(function_name, str) or not function_name:
            return tool_choice

        converted = dict(tool_choice)
        converted.pop("function", None)
        converted["type"] = converted.get("type", "function")
        converted["name"] = function_name
        return converted


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
