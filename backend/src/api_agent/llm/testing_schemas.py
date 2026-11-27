"""模型测试服务的请求与响应契约：覆盖多协议模型拉取、类型分类、可用性、工具调用、流式、结构化输出与模态。"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from api_agent.infrastructure.api import ApiInput, ApiModel, PageInfo

ModelProtocol = Literal["auto", "openai", "anthropic", "gemini"]
ResolvedModelProtocol = Literal["openai", "anthropic", "gemini"]
ModelTestRunSource = Literal["manual", "ai"]
ModelTestRunStatus = Literal["queued", "running", "completed", "cancelled", "error", "interrupted"]


def _coerce_utc_datetime(value: Any) -> Any:
    """规范化 naive datetime 为 UTC，同时兼容 JSON 中的 ISO 字符串。"""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


SafeUtcDatetime = Annotated[datetime, BeforeValidator(_coerce_utc_datetime)]


def _clean_model_id_list(value: list[str]) -> list[str]:
    """清理模型 ID 空白并按首次出现顺序去重。"""
    cleaned: list[str] = []
    for item in value:
        model_id = item.strip()
        if model_id and model_id not in cleaned:
            cleaned.append(model_id)
    if not cleaned:
        raise ValueError("至少需要指定一个有效模型 ID")
    return cleaned


def _dedupe_checks(value: list["ProbeCheckName"]) -> list["ProbeCheckName"]:
    """按首次出现顺序去重探测项，并保证至少保留一个有效探测项。"""
    cleaned = list(dict.fromkeys(value))
    if not cleaned:
        raise ValueError("至少需要指定一个探测项")
    return cleaned


ModelCategory = Literal[
    "chat",
    "reasoning",
    "embedding",
    "rerank",
    "image",
    "audio",
    "video",
    "moderation",
    "unknown",
]

ProbeStatus = Literal[
    "untested",
    "supported",
    "partial",
    "unsupported",
    "available",
    "unavailable",
    "inferred",
    "unknown",
    "error",
]

ProbeCheckName = Literal[
    "availability",
    "tool_calling",
    "input_modalities",
    "output_modalities",
    "streaming",
    "structured_output",
]


class CapabilityResult(ApiModel):
    """单项能力或模态的评估结果，显式区分实测结论、元数据声明与启发式推断。"""

    status: ProbeStatus = "untested"
    source: Literal["probed", "metadata", "inferred", "untested"] = "untested"
    latency_ms: float | None = None
    ttft_ms: float | None = None
    detail: str | None = None


class ModalityItem(ApiModel):
    """单个输入或输出模态的支持状态。"""

    modality: Literal["text", "image", "audio", "video", "file", "reasoning", "embedding", "json"]
    status: ProbeStatus = "untested"
    source: Literal["probed", "metadata", "inferred", "untested"] = "untested"
    detail: str | None = None


class ModelProfile(ApiModel):
    """单个模型的清单与能力画像；未实测字段保持 untested 或 inferred，不伪造通过。"""

    id: str
    owned_by: str | None = None
    created: int | None = None
    model_type: ModelCategory = "unknown"
    type_source: Literal["probed", "metadata", "inferred", "unknown"] = "unknown"
    availability: CapabilityResult = Field(default_factory=CapabilityResult)
    tool_calling: CapabilityResult = Field(default_factory=CapabilityResult)
    streaming: CapabilityResult = Field(default_factory=CapabilityResult)
    structured_output: CapabilityResult = Field(default_factory=CapabilityResult)
    input_modalities: list[ModalityItem] = Field(default_factory=list)
    output_modalities: list[ModalityItem] = Field(default_factory=list)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class ModelDiscoverRequest(ApiInput):
    """测试用户提供的 Base URL 与 API Key 能否拉取模型清单。"""

    base_url: str | None = Field(default=None, max_length=2000)
    api_key: str | None = Field(default=None, max_length=1000)
    use_saved_key: bool = False
    protocol: ModelProtocol = "auto"
    timeout_seconds: float = Field(default=20.0, ge=2.0, le=120.0)


class ModelDiscoverResponse(ApiModel):
    """模型拉取测试结果与初始画像清单。"""

    ok: bool
    protocol: ResolvedModelProtocol = "openai"
    base_url: str | None = None
    http_status: int | None = None
    latency_ms: float = 0.0
    count: int = 0
    message: str = ""
    models: list[ModelProfile] = Field(default_factory=list)


class ModelProbeRequest(ApiInput):
    """针对选定模型执行可用性、工具调用、流式响应、结构化输出与模态探测。"""

    base_url: str | None = Field(default=None, max_length=2000)
    api_key: str | None = Field(default=None, max_length=1000)
    use_saved_key: bool = False
    protocol: ModelProtocol = "auto"
    models: list[str] = Field(min_length=1, max_length=30)
    checks: list[ProbeCheckName] = Field(
        default_factory=lambda: ["availability", "tool_calling", "input_modalities"]
    )
    timeout_seconds: float = Field(default=30.0, ge=3.0, le=120.0)
    concurrency: int = Field(default=5, ge=1, le=10)

    @field_validator("models")
    @classmethod
    def _clean_models(cls, value: list[str]) -> list[str]:
        return _clean_model_id_list(value)


class ModelProbeResponse(ApiModel):
    """批量模型探测结果汇总。"""

    ok: bool
    protocol: ResolvedModelProtocol = "openai"
    base_url: str
    tested_count: int = 0
    available_count: int = 0
    tool_supported_count: int = 0
    streaming_supported_count: int = 0
    structured_output_supported_count: int = 0
    message: str = ""
    results: list[ModelProfile] = Field(default_factory=list)


class ModelTestRunStartRequest(ApiInput):
    """启动持久化后台模型探测任务请求（临时 api_key 仅驻留内存，绝不落库）。"""

    base_url: str | None = Field(default=None, max_length=2000)
    api_key: str | None = Field(default=None, max_length=1000)
    use_saved_key: bool = False
    protocol: ModelProtocol = "auto"
    models: list[str] = Field(min_length=1, max_length=100)
    checks: list[ProbeCheckName] = Field(
        default_factory=lambda: ["availability", "tool_calling", "input_modalities"],
        min_length=1,
    )
    timeout_seconds: float = Field(default=30.0, ge=3.0, le=120.0)
    concurrency: int = Field(default=4, ge=1, le=10)
    cooldown_ms: int = Field(default=500, ge=0, le=60_000)
    rounds: int = Field(default=1, ge=1, le=20)
    title: str | None = Field(default=None, max_length=200)
    source: ModelTestRunSource = "manual"
    badge: str | None = Field(default=None, max_length=40)
    discover_meta: dict[str, Any] | None = None
    discovered_profiles: list[ModelProfile] | None = None

    @field_validator("models")
    @classmethod
    def _clean_models(cls, value: list[str]) -> list[str]:
        return _clean_model_id_list(value)

    @field_validator("checks")
    @classmethod
    def _clean_checks(cls, value: list[ProbeCheckName]) -> list[ProbeCheckName]:
        return _dedupe_checks(value)


class ModelTestAiSuiteSpec(BaseModel):
    """AI 助手触发多轮模型能力探测套件的强类型规范。"""

    model_config = ConfigDict(str_strip_whitespace=True, hide_input_in_errors=True)

    base_url: str | None = Field(default=None, max_length=2000)
    api_key: str | None = Field(default=None, max_length=1000)
    use_saved_key: bool = True
    protocol: ModelProtocol = "auto"
    models: list[str] = Field(min_length=1, max_length=50)
    checks: list[ProbeCheckName] = Field(
        default_factory=lambda: ["availability", "tool_calling", "input_modalities"],
        min_length=1,
    )
    rounds: int = Field(default=1, ge=1, le=20)
    cooldown_ms: int = Field(default=500, ge=0, le=60_000)
    timeout_seconds: float = Field(default=30.0, ge=3.0, le=120.0)
    concurrency: int = Field(default=4, ge=1, le=10)
    title: str | None = Field(default=None, max_length=200)
    badge: str | None = Field(default=None, max_length=40)
    discover_meta: dict[str, Any] | None = None
    discovered_profiles: list[ModelProfile] | None = None

    @field_validator("models")
    @classmethod
    def _clean_models(cls, value: list[str]) -> list[str]:
        return _clean_model_id_list(value)

    @field_validator("checks")
    @classmethod
    def _clean_checks(cls, value: list[ProbeCheckName]) -> list[ProbeCheckName]:
        return _dedupe_checks(value)


class ModelTestSubCaseSnapshot(ApiModel):
    """单轮单个模型的只读回放快照，供历史查看与 AI 子用例链接点击回放。"""

    case_id: str
    round_index: int = 1
    case_index: int = 0
    label: str
    model_id: str
    checks: list[ProbeCheckName] = Field(default_factory=list)
    status: str = "unknown"
    latency_ms: float | None = None
    ttft_ms: float | None = None
    request_snapshot: dict[str, Any] = Field(default_factory=dict)
    observation: dict[str, Any] = Field(default_factory=dict)
    profile: ModelProfile
    created_at: SafeUtcDatetime | None = None


class ModelTestRunSummary(ApiModel):
    """单次模型探测任务（支持多轮）的汇总指标。"""

    tested_count: int = 0
    total_cases: int = 0
    completed_cases: int = 0
    available_count: int = 0
    unavailable_count: int = 0
    error_count: int = 0
    tool_supported_count: int = 0
    streaming_supported_count: int = 0
    structured_output_supported_count: int = 0
    vision_supported_count: int = 0
    avg_latency_ms: float | None = None
    avg_ttft_ms: float | None = None
    completed_rounds: int = 0
    total_rounds: int = 1
    message: str = ""


class ModelTestRunListItem(ApiModel):
    """持久化模型探测历史列表项（不包含明文密钥）。"""

    id: str
    title: str | None = None
    source: ModelTestRunSource = "manual"
    badge: str | None = None
    status: ModelTestRunStatus = "queued"
    error_message: str | None = None
    base_url: str
    protocol: ModelProtocol = "auto"
    resolved_protocol: ResolvedModelProtocol | None = None
    used_saved_key: bool = False
    has_custom_api_key: bool = False
    timeout_seconds: float = 20.0
    concurrency: int = 4
    checks: list[ProbeCheckName] = Field(default_factory=list)
    target_models: list[str] = Field(default_factory=list)
    cooldown_ms: int = 500
    total_rounds: int = 1
    completed_rounds: int = 0
    sub_case_count: int = 0
    summary: ModelTestRunSummary | None = None
    summary_text: str | None = None
    started_at: SafeUtcDatetime | None = None
    finished_at: SafeUtcDatetime | None = None
    created_at: SafeUtcDatetime
    updated_at: SafeUtcDatetime


class ModelTestRunDetail(ModelTestRunListItem):
    """单次模型探测任务完整详情，包含最终画像快照、各轮汇总与子用例快照。"""

    discover_meta: dict[str, Any] | None = None
    models_snapshot: list[ModelProfile] = Field(default_factory=list)
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    sub_cases: list[ModelTestSubCaseSnapshot] = Field(default_factory=list)


class ModelTestRunListResponse(ApiModel):
    """分页的模型探测任务历史列表响应。"""

    items: list[ModelTestRunListItem] = Field(default_factory=list)
    page: PageInfo
