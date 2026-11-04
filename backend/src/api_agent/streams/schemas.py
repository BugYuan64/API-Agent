"""Input and response contracts for long-connection test sessions."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from api_agent.infrastructure.api import ApiInput, ApiModel, UtcDatetime
from api_agent.streams.state import Kind


class StreamStart(ApiInput):
    """启动一次长连接测试的输入契约。

    作用：把 SSE 与 WebSocket 的差异收敛到 ``kind`` 一个开关上——
    两者共享 URL、头、超时与上限，只在 ``kind`` 上分流，
    界面因此可以用一套表单覆盖两种协议。
    实现：``send`` 只在 WebSocket 下有效（SSE 是单向流，服务端不接受客户端消息），
    由 ``valid_kind`` 拒绝在 SSE 上携带待发消息，避免“发了但被忽略”的错觉。
    """

    kind: Kind
    url: str = Field(min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    subprotocols: list[str] = Field(default_factory=list, max_length=5)
    max_events: int = Field(default=200, ge=1, le=2000)
    max_duration_ms: int = Field(default=60_000, ge=1000, le=900_000)
    connect_timeout_ms: int = Field(default=10_000, ge=100, le=60_000)
    ping_interval_ms: int = Field(default=20_000, ge=0, le=300_000)
    send: list[str] = Field(default_factory=list, max_length=20)
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

    @model_validator(mode="after")
    def valid_kind(self):
        """拒绝“SSE 上携带待发消息”这类不可能生效的组合。

        实现：SSE 是服务器到客户端的单向流，没有客户端写入通道，
        因此带了 ``send`` 就说明用户预期与实际行为不符，应在提交前就报错。
        """
        if self.kind == "sse" and self.send:
            raise ValueError("SSE streams cannot send messages; use the websocket kind")
        return self


class StreamSessionResponse(ApiModel):
    """长连接会话的当前状态。

    实现：``finished`` 是派生值（终态判定），让前端不必自己枚举三种终态；
    ``event_count``/``byte_count``/``sent_count`` 由服务端记账，
    前端不需要遍历事件再统计一遍。
    """

    id: str
    kind: Literal["sse", "websocket"]
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


class StreamHistoryResponse(ApiModel):
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
