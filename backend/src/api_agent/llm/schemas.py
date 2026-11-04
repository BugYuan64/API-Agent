"""OpenAI-compatible request and response schemas, including tool/function definitions."""

from typing import Any, Literal

from pydantic import Field

from api_agent.infrastructure.api import ApiInput, ApiModel


class FunctionCall(ApiModel):
    """大模型发起的单个函数调用。

    实现：``arguments`` 在解析阶段已将模型返回的 JSON 字符串反序列化为结构化字典，
    调用方可直接传参执行，不必在业务层再次 ``json.loads``。
    """

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCall(ApiModel):
    """OpenAI 规范的工具调用对象。

    实现：包含唯一的 ``id`` 与函数签名，后续工具执行结果需以此 ``id``
    回传作为 role="tool" 的 ``tool_call_id``。
    """

    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(ApiInput):
    """对话消息，支持系统提示、用户输入、模型回复与工具执行结果四种角色。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


class FunctionDefinition(ApiInput):
    """向大模型声明的单个工具函数元信息与参数 JSON Schema。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolDefinition(ApiInput):
    """OpenAI 格式的工具包装（当前类型固定为 function）。"""

    type: Literal["function"] = "function"
    function: FunctionDefinition


class ChatCompletionRequest(ApiInput):
    """向 OpenAI 兼容接口发起单次请求的输入体（支持工具定义）。

    实现：``model`` 可选，未传时默认使用配置项 ``LLM_MODEL``；
    ``tools`` 传入支持的函数列表，模型可自主决定是直接回复文本还是调用工具；
    ``tool_choice`` 允许强制调用特定工具或交由模型决定（auto/required/none）。
    """

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)


class ChatCompletionResponse(ApiModel):
    """OpenAI 兼容接口的结构化响应（非流式 Response 模式）。

    作用：统一提取并暴露模型返回的文本正文、发起的工具调用清单以及 Token 消耗统计。
    """

    id: str
    model: str
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class LLMStatusResponse(ApiModel):
    """当前大模型接口配置与可用性状态（脱敏暴露）。"""

    configured: bool
    mode: str
    base_url: str | None = None
    model: str
    has_api_key: bool


class LLMConfigResponse(ApiModel):
    """前端设置页面获取的大模型配置详情（API Key 脱敏）。"""

    enabled: bool
    provider: str = "openai"
    base_url: str | None = None
    model: str = "gpt-4o"
    timeout_seconds: float = 60.0
    has_api_key: bool = False


class LLMConfigUpdate(ApiInput):
    """前端设置页面提交的大模型配置更新体。

    实现：``api_key`` 传 None 时仅对同源保留原密钥；空字符串清空密钥；
    非空字符串更新为新密钥；支持动态修改 base_url 与模型名。
    """

    enabled: bool = False
    provider: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    model: str = Field(default="gpt-4o", min_length=1, max_length=120)
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)


class LLMPingRequest(ApiInput):
    """连通性测试请求参数，支持测试未保存的预览配置或已保存的配置。"""

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class LLMPingResponse(ApiModel):
    """大模型接口连通性探测结果。"""

    ok: bool
    latency_ms: float
    message: str | None = None


class LLMModelListRequest(ApiInput):
    """查询可用模型列表请求入参，支持使用未保存的临时参数或数据库已存配置。"""

    base_url: str | None = None
    api_key: str | None = None


class LLMModelListResponse(ApiModel):
    """大模型服务返回的可用模型列表。"""

    ok: bool
    models: list[str] = Field(default_factory=list)
    count: int = 0
    message: str | None = None
