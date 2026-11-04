"""Validate supplier shapes before translating them into our public contract."""

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from api_agent.llm.errors import LLMResponseError
from api_agent.llm.schemas import ChatCompletionResponse, FunctionCall, ToolCall


class _Message(BaseModel):
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class _Choice(BaseModel):
    message: _Message
    finish_reason: str | None = None


class _Completion(BaseModel):
    choices: list[_Choice] = Field(min_length=1)
    usage: dict[str, Any] | None = None


def parse_completion(raw: Any, model: str) -> ChatCompletionResponse:
    """Normalize text, tools and numeric usage without leaking rejected payloads."""
    try:
        parsed = _Completion.model_validate(raw)
        choice = parsed.choices[0]
        calls = []
        for call in choice.message.tool_calls or []:
            function = call.get("function")
            if not isinstance(function, dict):
                raise ValueError("invalid_function")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    # Preserve the existing malformed-JSON diagnostic contract.
                    arguments = {"_raw": arguments}
            calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    type=call.get("type", "function"),
                    function=FunctionCall(
                        name=str(function.get("name") or ""), arguments=arguments
                    ),
                )
            )
        result = ChatCompletionResponse(
            id=str(raw.get("id") or ""),
            model=str(raw.get("model") or model),
            content=choice.message.content,
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            usage={
                name: count
                for name, count in (parsed.usage or {}).items()
                if type(count) is int and count >= 0
            },
        )
        # Pydantic accepts Python strings containing lone surrogates and floats
        # such as NaN in tool arguments; neither belongs in our UTF-8 JSON API.
        json.dumps(result.model_dump(), ensure_ascii=False, allow_nan=False).encode("utf-8")
        return result
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise LLMResponseError("LLM response has an invalid completion structure") from None


def parse_models(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        items = raw.get("data") or raw.get("models") or []
    else:
        items = raw
    if not isinstance(items, list):
        raise LLMResponseError("LLM response has an invalid model list structure")
    names = set()
    for item in items:
        name = item.get("id") or item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    try:
        for name in names:
            name.encode("utf-8")
    except UnicodeError:
        raise LLMResponseError("LLM response contains an invalid model identifier") from None
    return sorted(names)
