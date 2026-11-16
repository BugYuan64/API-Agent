"""定义 OpenAI 兼容对话、工具与动态配置接口的数据结构。

依赖 Pydantic 和公共 API 模型；输入约束字段结构及范围，供应商响应
由解析器规范化，密钥复用与实际工具执行均不由这些结构决定。
"""

from typing import Any, Literal

from pydantic import Field

from api_agent.infrastructure.api import ApiInput, ApiModel


class FunctionCall(ApiModel):
    """承载模型建议调用的函数名及参数字典，不自动执行或校验函数业务语义。

    解析器通常解码 JSON 字符串；无法解析时可能保存 _raw 诊断字段，
    调用方执行前仍须验证参数与权限。
    """

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCall(ApiModel):
    """承载工具调用 ID、固定 function 类型与函数参数，供工具结果关联。

    ID 来自供应商，结构本身不保证全局唯一或非空。
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
    """向 OpenAI 兼容 Chat Completions 接口发送的非流式输入。

    model 未传时使用客户端有效默认模型；tools 仅声明函数，tool_choice
    原样传给供应商，具体语义及支持情况由供应商决定。
    """

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)


class ChatCompletionResponse(ApiModel):
    """统一暴露非流式 Chat Completions 的文本、工具调用、停止原因和用量。

    usage 只含已接受的供应商统计，缺失字段不能作为零成本证明。
    """

    id: str
    model: str
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class LLMStatusResponse(ApiModel):
    """展示有效模型配置的本地就绪状态及密钥是否存在，不代表网络验证结果。"""

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
    """返回真实对话探测的成功标志、经过时间及结果说明，不涵盖全部模型能力。"""

    ok: bool
    latency_ms: float
    message: str | None = None


class LLMModelListRequest(ApiInput):
    """查询可用模型列表请求入参，支持使用未保存的临时参数或数据库已存配置。"""

    base_url: str | None = None
    api_key: str | None = None


class LLMModelListResponse(ApiModel):
    """返回供应商报告的模型清单、数量及查询结果，不保证各模型可实际调用。"""

    ok: bool
    models: list[str] = Field(default_factory=list)
    count: int = 0
    message: str | None = None
