"""把供应商 JSON 响应校验并转换为应用公开模型。

依赖 Pydantic 和共享 LLMResponseError；规范文本、工具参数与用量，
拒绝非法结构时不把原始响应附入异常，不执行任何模型工具调用。
"""

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from api_agent.llm.errors import LLMResponseError
from api_agent.llm.schemas import ChatCompletionResponse, FunctionCall, ToolCall


class _Message(BaseModel):
    """校验供应商消息中的可空文本及原始工具调用列表，不承担工具参数语义验证。"""

    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class _Choice(BaseModel):
    """承载单条供应商候选消息和停止原因，供完成响应解析使用。"""

    message: _Message
    finish_reason: str | None = None


class _Completion(BaseModel):
    """要求至少一条候选消息，保留待进一步筛选的原始 usage 字段。"""

    choices: list[_Choice] = Field(min_length=1)
    usage: dict[str, Any] | None = None


def parse_completion(raw: Any, model: str) -> ChatCompletionResponse:
    """从供应商响应的首个候选提取文本、工具调用与非负整数 usage。

    工具参数 JSON 解析失败时保留 _raw 诊断字段；无效结构、非有限值
    或不可编码内容统一抛 LLMResponseError，不回显被拒绝的响应正文。
    """
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
    """从列表或 data/models 包装中提取非空模型 ID，返回去重排序结果。

    忽略无效项，列表外结构或不可 UTF-8 编码的 ID 抛 LLMResponseError；
    清单存在不代表每个模型都已通过调用验证。
    """
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
