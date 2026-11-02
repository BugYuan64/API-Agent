"""Response contracts for long-connection test sessions."""

from typing import Literal

from pydantic import Field

from api_agent.infrastructure.api import ApiInput, ApiModel, UtcDatetime


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
