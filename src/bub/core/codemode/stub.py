"""Render tools as a Python stub that tells model-written code their signatures and result types."""

from __future__ import annotations

import keyword
import re
from collections.abc import Iterable
from typing import Any

from bub.core.tools import Tool, model_tools

_STUB_HEADER = '''"""Tools available inside `run_code` as `tools.<name>(...)`.

Every tool is an async function: `await` it (top-level `await` is allowed) and pass keyword
arguments only. Each call returns the structured value described by its return type and
raises an exception when the tool fails. Use `asyncio.gather` to run independent calls
concurrently.

The `republic` module is also available (preset as a global and importable); it is documented
at the end of this file.
"""

from typing import Any, Literal, NotRequired, TypedDict

import republic'''

_REPUBLIC_STUB = """# ---------------------------------------------------------------------------
# `republic`: media and models inside `run_code`. Models are served by the host with
# its credentials and settings; every model method is async. Only the members below exist.
#
# class republic.Image / republic.Audio / republic.Video:
#     __init__(media_type: str, data: bytes | None = None, url: str | None = None)
#     media_type: str; data: bytes | None; url: str | None
#     base64_data: str; data_url: str
#     `tools.attach_image/audio/video` also take a file path in the environment, an http(s)/gs URL
#     or a data URL instead, and load it for you.
#
# republic.system(text: str) -> Message
# republic.user(*content: str | Image | Audio | Video) -> Message
# republic.assistant(*content: str | Image | Audio | Video) -> Message
#     Message has .role, .content (list of text and media) and .text.
#
# republic.get_model(spec: str | None = None) -> ChatModel
#     A "provider:model" spec; None is the model running this session.
#     await model.chat(prompt: str | Message | list[str | Message], **options) -> response
#         options: max_tokens, temperature, top_p, top_k, stop, seed, reasoning_effort, extra_body...
#         (no tools). A plain string is a user message.
#         response.text, .reasoning, .refusal, .finish_reason, .model,
#         .token_usage.input_tokens / .output_tokens, .message (append it to continue the conversation)
#     model.stream(prompt, **options) -> Stream   # same arguments as chat(); not awaited
#         async with model.stream(...) as stream:
#             async for event in stream: ...
#         Events from republic.events: TextDelta(.chunk), ReasoningDelta(.chunk), RefusalDelta(.chunk),
#         UsageDelta(.usage), ImageReady(.image), CitationAdded(.citation), Completed(.response, last).
#         After iterating: stream.response (as returned by chat()), stream.text, .reasoning, .token_usage.
# republic.get_embedding_model(spec: str) -> EmbeddingModel
#     await model.embed(text: str, *, dimensions: int | None = None) -> response   # response.vector
#     await model.embed_many(texts: list[str], *, dimensions: int | None = None) -> response   # response.vectors
# republic.get_decision_model(spec: str) -> DecisionModel
#     await model.decide(state, *, questions: dict[str, Question]) -> response
#         Questions from republic.decisions: Noul(instructions, criteria=None) (yes/no),
#         Choice(instructions, criteria) (pick one), Score(instructions, criteria) (ordered levels).
#         response.answers[id] or response.<id>: .noul | .choice/.probabilities/.confidence | .score/...
#
# Example:
#     model = republic.get_model()
#     chart = republic.Image("image/png", data=open("chart.png", "rb").read())
#     reply = await model.chat([republic.user("Describe this chart.", chart)])
#     print(reply.text)
#     async with model.stream("Write a haiku.") as stream:
#         async for event in stream:
#             if isinstance(event, republic.events.TextDelta):
#                 print(event.chunk, end="")"""
_JSON_TYPES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "None"}


def _identifier(name: str) -> str:
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit() or keyword.iskeyword(ident):
        ident = f"_{ident}"
    return ident


def _is_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9A-Za-z]+", name))


_MEDIA_FIELDS = {"media_type", "data", "url"}


def _media_class(schema: dict[str, Any]) -> str | None:
    """Recognize the JSON schema of ``republic.Image``, ``Audio`` and ``Video`` parameters."""
    title = schema.get("title")
    properties = schema.get("properties")
    if title in ("Image", "Audio", "Video") and isinstance(properties, dict) and set(properties) == _MEDIA_FIELDS:
        return str(title)
    return None


def _docstring(text: str, indent: str = "    ") -> list[str]:
    text = text.strip().replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if not text:
        return []
    lines = text.splitlines()
    if len(lines) == 1:
        return [f'{indent}"""{lines[0]}"""']
    return [f'{indent}"""{lines[0]}', *(f"{indent}{line}".rstrip() for line in lines[1:]), f'{indent}"""']


class _StubBuilder:
    """Render tool JSON schemas as Python stub declarations."""

    def __init__(self) -> None:
        self.classes: dict[str, str] = {}
        self._refs: dict[tuple[int, str], str] = {}

    def function(self, tool_item: Tool) -> str:
        name = _identifier(tool_item.name)
        schema = tool_item.parameters or {}
        defs = self._defs(schema)
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        params: list[str] = []
        arg_docs: list[str] = []
        for key, prop in properties.items():
            hint = self.type_hint(prop, defs, f"{_pascal(name)}{_pascal(key)}")
            if key in required:
                params.append(f"{key}: {hint}")
            elif isinstance(prop, dict) and "default" in prop:
                params.append(f"{key}: {hint} = {prop['default']!r}")
            else:
                params.append(f"{key}: {hint} = ...")
            description = prop.get("description") if isinstance(prop, dict) else None
            if description:
                arg_docs.append(f"    {key}: {' '.join(description.split())}")
        if not all(_is_identifier(key) for key in properties):
            signature = "**kwargs: Any"
        elif params:
            signature = f"*, {', '.join(params)}"
        else:
            signature = ""

        output = tool_item.output_schema
        returns = "Any" if output is None else self.type_hint(output, self._defs(output), f"{_pascal(name)}Result")
        doc = tool_item.description.strip()
        if arg_docs:
            doc = f"{doc}\n\nArgs:\n" + "\n".join(arg_docs) if doc else "Args:\n" + "\n".join(arg_docs)
        body = _docstring(doc) or ["    ..."]
        return "\n".join([f"async def {name}({signature}) -> {returns}:", *body])

    def type_hint(  # noqa: C901
        self, schema: Any, defs: dict[str, Any], name_hint: str, ref_key: tuple[int, str] | None = None
    ) -> str:
        if not isinstance(schema, dict) or not schema:
            return "Any"
        if (media := _media_class(schema)) is not None:
            return f"republic.{media}"
        if local_defs := self._defs(schema):
            # Schemas of single parameters carry their own definitions.
            defs = {**defs, **local_defs}
        if isinstance(ref := schema.get("$ref"), str):
            return self._ref(ref, defs)
        for key in ("anyOf", "oneOf"):
            if isinstance(options := schema.get(key), list):
                return self._union(self.type_hint(option, defs, name_hint) for option in options)
        if isinstance(all_of := schema.get("allOf"), list) and len(all_of) == 1:
            return self.type_hint(all_of[0], defs, name_hint, ref_key)
        if "const" in schema:
            return f"Literal[{schema['const']!r}]"
        if isinstance(enum := schema.get("enum"), list) and enum:
            return f"Literal[{', '.join(repr(value) for value in enum)}]"

        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            return self._union(self.type_hint({**schema, "type": item}, defs, name_hint) for item in schema_type)
        if schema_type == "array":
            if isinstance(prefix := schema.get("prefixItems"), list):
                return f"tuple[{', '.join(self.type_hint(item, defs, name_hint) for item in prefix)}]"
            return f"list[{self.type_hint(schema.get('items'), defs, f'{name_hint}Item')}]"
        if schema_type == "object":
            if isinstance(properties := schema.get("properties"), dict) and properties:
                return self._typed_dict(schema, properties, defs, schema.get("title") or name_hint, ref_key)
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict) and extra:
                return f"dict[str, {self.type_hint(extra, defs, f'{name_hint}Value')}]"
            return "dict[str, Any]"
        return _JSON_TYPES.get(schema_type, "Any") if isinstance(schema_type, str) else "Any"

    @staticmethod
    def _defs(schema: dict[str, Any]) -> dict[str, Any]:
        defs = schema.get("$defs", schema.get("definitions"))
        return defs if isinstance(defs, dict) else {}

    @staticmethod
    def _union(hints: Iterable[str]) -> str:
        unique = list(dict.fromkeys(hints))
        return "Any" if "Any" in unique else " | ".join(unique)

    def _ref(self, ref: str, defs: dict[str, Any]) -> str:
        name = ref.rsplit("/", 1)[-1]
        key = (id(defs), name)
        if key in self._refs:
            return self._refs[key]
        target = defs.get(name)
        if not isinstance(target, dict):
            return "Any"
        hint = self.type_hint(target, defs, name, ref_key=key)
        self._refs[key] = hint
        return hint

    def _typed_dict(
        self,
        schema: dict[str, Any],
        properties: dict[str, Any],
        defs: dict[str, Any],
        title: str,
        ref_key: tuple[int, str] | None,
    ) -> str:
        base = _identifier(_pascal(title) or "Result")
        class_name = base
        index = 2
        while class_name in self.classes:
            class_name = f"{base}{index}"
            index += 1
        # Reserve the name first so recursive references resolve to this class.
        self.classes[class_name] = ""
        if ref_key is not None:
            self._refs[ref_key] = class_name

        required = set(schema.get("required") or [])
        fields: list[tuple[str, str, str | None]] = []
        for key, prop in properties.items():
            hint = self.type_hint(prop, defs, f"{class_name}{_pascal(key)}")
            if key not in required:
                hint = f"NotRequired[{hint}]"
            description = prop.get("description") if isinstance(prop, dict) else None
            fields.append((key, hint, description))
        source = self._class_source(class_name, fields, schema.get("description"))

        # Reuse an identical class that was declared under the same title by another tool.
        for existing, existing_source in self.classes.items():
            if (
                existing != class_name
                and re.fullmatch(rf"{re.escape(base)}\d*", existing)
                and existing_source == source.replace(class_name, existing)
            ):
                del self.classes[class_name]
                if ref_key is not None:
                    self._refs[ref_key] = existing
                return existing
        self.classes[class_name] = source
        return class_name

    @staticmethod
    def _class_source(name: str, fields: list[tuple[str, str, str | None]], description: object) -> str:
        if not all(_is_identifier(key) for key, _, _ in fields):
            items = ", ".join(f"{key!r}: {hint}" for key, hint, _ in fields)
            return f"{name} = TypedDict({name!r}, {{{items}}})"
        lines = [f"class {name}(TypedDict):"]
        if isinstance(description, str):
            lines.extend(_docstring(description))
        for key, hint, field_description in fields:
            if field_description:
                lines.append(f"    # {' '.join(field_description.split())}")
            lines.append(f"    {key}: {hint}")
        return "\n".join(lines)


def render_tool_stub(tools: Iterable[Tool]) -> str:
    """Render a Python stub declaring one function per model-facing tool."""
    builder = _StubBuilder()
    functions = [builder.function(tool_item) for tool_item in model_tools(tools)]
    return "\n\n\n".join([_STUB_HEADER, *builder.classes.values(), *functions, _REPUBLIC_STUB]) + "\n"
