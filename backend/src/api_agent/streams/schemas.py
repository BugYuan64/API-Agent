"""Input and response contracts for long-connection test sessions and AI suites."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from api_agent.infrastructure.api import ApiInput, ApiModel, UtcDatetime
from api_agent.infrastructure.outcomes import OutcomeFields


class StreamStart(ApiInput):
    """启动有界 WebSocket 调试；SSE 已归入 HTTP 接口测试。"""

    kind: Literal["websocket"] = "websocket"
    url: str = Field(min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    subprotocols: list[str] = Field(default_factory=list, max_length=5)
    max_events: int = Field(default=200, ge=1, le=2000)
    max_duration_ms: int = Field(default=60_000, ge=1000, le=900_000)
    connect_timeout_ms: int = Field(default=10_000, ge=100, le=60_000)
    ping_interval_ms: int = Field(default=20_000, ge=0, le=300_000)
    send: list[Annotated[str, Field(max_length=65_536)]] = Field(
        default_factory=list, max_length=20
    )
    send_interval_ms: int = Field(default=500, ge=0, le=60_000)
    project_id: str | None = None

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """在提交阶段就执行与控制台相同的请求头策略。

        作用：长连接此前只限制头的数量，``Host``、``Connection``、
        ``Sec-WebSocket-Key`` 等由传输层自行管理的头可以被用户覆盖，
        从而改变握手语义或破坏报文结构。这里复用同一份
        ``validate_headers``，让两条对外请求路径的边界完全一致。
        实现：策略拒绝（保留头/非法头/重复头/超限）以 ``ValueError`` 抛出，
        由 pydantic 统一转成 422，不会走到真实连接。
        """
        from api_agent.infrastructure.targets import TargetError, validate_headers

        try:
            validate_headers(value)
        except TargetError as error:
            raise ValueError(str(error)) from None
        return value

    @field_validator("subprotocols")
    @classmethod
    def valid_subprotocols(cls, values: list[str]) -> list[str]:
        """校验子协议是 RFC 6455 允许的 token 且不重复。

        实现：逐项使用 token 字符集匹配（与头名规则一致），
        重复会让握手产生歧义，直接拒绝。
        """
        import re

        token = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
        if len(set(values)) != len(values) or any(not token.fullmatch(v) for v in values):
            raise ValueError("subprotocols must be unique HTTP tokens")
        return values


class StreamSessionResponse(ApiModel, OutcomeFields):
    """长连接会话的当前状态。

    实现：``finished`` 是派生值（终态判定），让前端不必自己枚举三种终态；
    ``event_count``/``byte_count``/``sent_count`` 由服务端记账，
    前端不需要遍历事件再统计一遍。
    """

    id: str
    kind: Literal["websocket", "sse"]
    url: str
    status: str
    finished: bool
    stop_reason: str | None
    error_kind: str | None
    error_message: str | None
    http_status: int | None
    content_type: str | None
    subprotocol: str | None
    close_code: int | None
    close_reason: str | None
    event_count: int
    byte_count: int
    sent_count: int
    truncated_count: int
    opened_at: UtcDatetime | None
    closed_at: UtcDatetime | None
    source: str = "manual"
    title: str | None = None
    badge: str | None = None
    parent_session_id: str | None = None
    round_index: int = 1
    case_index: int = 0
    case_label: str | None = None
    cooldown_ms: int = 500
    total_rounds: int = 1
    summary: str | None = None
    sub_case_count: int = 0


class StreamEventResponse(ApiModel):
    """长连接上的一条事件（SSE 消息、WebSocket 帧或生命周期标记）。

    实现：``sequence`` 用作增量游标，必须原样回传不能重新编号；
    ``direction`` 区分收（in）发（out）；``kind`` 说明是
    message/open/close/comment/text/binary/error。
    """

    session_id: str
    sequence: int
    direction: str
    kind: str
    event_name: str | None
    data: str | None
    data_bytes: int
    truncated: bool
    elapsed_ms: int


class StreamEventPage(ApiModel):
    """一次增量读取的结果。

    实现：``next_cursor`` 直接作为下一次请求的 ``after``；
    ``finished`` 表示会话结束，``has_more`` 表示仍有下一页事件。
    """

    session: StreamSessionResponse
    events: list[StreamEventResponse] = Field(default_factory=list)
    next_cursor: int = 0
    finished: bool = False
    has_more: bool = False


class StreamSendRequest(ApiInput):
    """向 WebSocket 会话发送一条文本消息。

    实现：长度上限与事件存储上限一致，避免落库时被迫截断。
    """

    message: str = Field(min_length=1, max_length=65_536)


class StreamHistoryResponse(ApiModel, OutcomeFields):
    """数据库中的历史会话摘要（进程重启后仍可回看）。

    实现：请求头已脱敏；``event_count`` 是落库时的最终计数。
    """

    id: str
    kind: str
    url: str
    request_headers: dict
    status: str
    stop_reason: str | None
    error_kind: str | None
    error_message: str | None
    http_status: int | None
    content_type: str | None
    subprotocol: str | None
    close_code: int | None
    close_reason: str | None
    event_count: int
    byte_count: int
    sent_count: int
    truncated_count: int
    settings: dict
    opened_at: UtcDatetime | None
    closed_at: UtcDatetime | None
    created_at: UtcDatetime
    source: str = "manual"
    title: str | None = None
    badge: str | None = None
    parent_session_id: str | None = None
    round_index: int = 1
    case_index: int = 0
    case_label: str | None = None
    cooldown_ms: int = 500
    total_rounds: int = 1
    summary: str | None = None
    sub_case_count: int = 0


class StreamReplayInputs(ApiModel):
    """Saved startup parameters for a single WebSocket session."""

    url: str
    request_headers: dict
    subprotocols: list[str]
    scheduled_messages: list[str] | None


class StreamAiCaseSpec(ApiInput):
    """AI 驱动的多轮 WebSocket 套件中的单条测试子用例规格。"""

    label: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    subprotocols: list[str] = Field(default_factory=list, max_length=5)
    send: list[Annotated[str, Field(max_length=65_536)]] = Field(
        default_factory=list, max_length=20
    )
    send_interval_ms: int = Field(default=200, ge=0, le=60_000)
    max_events: int = Field(default=50, ge=1, le=2000)
    max_duration_ms: int = Field(default=5_000, ge=1000, le=900_000)
    connect_timeout_ms: int = Field(default=10_000, ge=100, le=60_000)
    ping_interval_ms: int = Field(default=20_000, ge=0, le=300_000)
    expect_contains: str | None = Field(default=None, max_length=500)
    expect_min_events: int | None = Field(default=None, ge=0, le=2000)
    expect_close_code: int | None = Field(default=None, ge=1000, le=4999)
    expect_handshake_reject: bool = False

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """复用 StreamStart 请求头安全校验，禁止覆盖传输层保留头。"""
        return StreamStart.safe_headers(value)

    @field_validator("subprotocols")
    @classmethod
    def valid_subprotocols(cls, values: list[str]) -> list[str]:
        """复用 StreamStart 子协议校验。"""
        return StreamStart.valid_subprotocols(values)


class StreamAiSuiteSpec(ApiInput):
    """AI 驱动的多轮 WebSocket 测试套件启动规格。"""

    url: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=200)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    subprotocols: list[str] = Field(default_factory=list, max_length=5)
    cases: list[StreamAiCaseSpec] = Field(min_length=1, max_length=50)
    rounds: int = Field(default=1, ge=1, le=20)
    cooldown_ms: int = Field(default=500, ge=0, le=60_000)
    project_id: str | None = None
    badge: str = Field(default="AI 测试", max_length=40)

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """复用 StreamStart 请求头安全校验。"""
        return StreamStart.safe_headers(value)

    @field_validator("subprotocols")
    @classmethod
    def valid_subprotocols(cls, values: list[str]) -> list[str]:
        """复用 StreamStart 子协议校验。"""
        return StreamStart.valid_subprotocols(values)


SUB_CASE_VERDICT_STATUSES = frozenset({"passed", "failed", "error", "cancelled", "interrupted"})


def effective_sub_case_status(record) -> str:
    """返回子用例对外一致的裁定状态，优先读取持久化在 settings 中的 case_status。"""
    stored_status = (getattr(record, "settings", None) or {}).get("case_status")
    if isinstance(stored_status, str) and stored_status in SUB_CASE_VERDICT_STATUSES:
        return stored_status
    return str(getattr(record, "status", "closed") or "closed")


def sub_case_notes(record) -> list[str]:
    """提取子用例持久化在 settings 中的断言说明列表，忽略空项。"""
    raw = (getattr(record, "settings", None) or {}).get("case_notes")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


class StreamSubCaseSummary(ApiModel, OutcomeFields):
    """归属于一条 AI WebSocket 历史记录的单个子用例摘要。"""

    id: str
    parent_session_id: str
    round_index: int
    case_index: int
    case_label: str | None = None
    label: str
    url: str
    status: str
    stop_reason: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    subprotocol: str | None = None
    close_code: int | None = None
    close_reason: str | None = None
    event_count: int = 0
    byte_count: int = 0
    sent_count: int = 0
    truncated_count: int = 0
    notes: list[str] = Field(default_factory=list)
    opened_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    created_at: UtcDatetime


class StreamSubCaseListResponse(ApiModel):
    """一条分组 AI WebSocket 历史下的全部子用例列表。"""

    session_id: str
    items: list[StreamSubCaseSummary] = Field(default_factory=list)
    total: int = 0


class StreamSubCaseReplayResponse(ApiModel):
    """单条 WebSocket 子用例的完整只读回放快照（连接参数 + 会话状态 + 事件流）。"""

    session_id: str
    case_id: str
    round_index: int
    case_index: int
    case_label: str | None = None
    label: str
    readonly: bool = True
    transport_status: str | None = None
    notes: list[str] = Field(default_factory=list)
    inputs: StreamReplayInputs
    session: StreamSessionResponse
    events: list[StreamEventResponse] = Field(default_factory=list)


class ActiveStreamItem(ApiModel):
    """跨页切回重连时暴露的单个活跃长连接或 AI 套件运行摘要。"""

    id: str
    kind: Literal["manual", "ai_suite"]
    source: str = "manual"
    url: str
    title: str | None = None
    badge: str | None = None
    status: str
    stage: str | None = None
    current_round: int = 1
    total_rounds: int = 1
    completed_cases: int = 0
    total_cases: int = 0
    cooldown_ms: int = 500
    cooldown_remaining_ms: int | None = None
    opened_at: UtcDatetime | None = None


class ActiveStreamsResponse(ApiModel):
    """当前正在后台运行的手动长连接与 AI 多轮测试套件列表。"""

    items: list[ActiveStreamItem] = Field(default_factory=list)
    manual_sessions: list[ActiveStreamItem] = Field(default_factory=list)
    ai_suites: list[ActiveStreamItem] = Field(default_factory=list)


class StreamSuiteStatusResponse(ApiModel):
    """AI WebSocket 多轮测试套件的状态与进度快照。"""

    id: str
    status: str
    stage: str
    source: str = "ai"
    title: str | None = None
    badge: str | None = None
    url: str
    current_round: int = 1
    total_rounds: int = 1
    completed_cases: int = 0
    total_cases: int = 0
    cooldown_ms: int = 500
    cooldown_remaining_ms: int | None = None
    summary: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
