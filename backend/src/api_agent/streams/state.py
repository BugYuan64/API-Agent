"""In-memory stream state and its public serialization; no database or transport IO."""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from api_agent.infrastructure.orm import as_utc, utc_now

MAX_EVENT_CHARS = 64 * 1024
Kind = Literal["sse", "websocket"]
Status = Literal["connecting", "open", "closed", "error", "cancelled"]


@dataclass
class StreamEvent:
    """内存时间线中的一条事件（与持久化行一一对应）。

    实现：``data`` 只保留前 64KiB，``data_bytes`` 记录真实长度，
    ``truncated`` 标记是否被截断，避免一条巨型事件占满前端与数据库。
    """

    sequence: int
    direction: str
    kind: str
    event_name: str | None
    data: str | None
    data_bytes: int
    truncated: bool
    elapsed_ms: int


@dataclass
class ActiveStream:
    """一个在跑的长连接会话的内存状态。

    作用：把“时间线缓冲 + 等待通知 + 结束原因”打包，供轮询接口增量读取。
    实现：``events`` 用 ``deque(maxlen=...)`` 做环形缓冲，超限自动丢弃最旧的，
    因此内存占用与已收事件数无关；``notify`` 是 ``asyncio.Event``，
    有新事件或状态变化时置位，长轮询据此立即返回。
    """

    id: str
    kind: Kind
    url: str
    status: Status = "connecting"
    events: deque[StreamEvent] = field(default_factory=lambda: deque(maxlen=4000))
    notify: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    stop_reason: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    subprotocol: str | None = None
    close_code: int | None = None
    close_reason: str | None = None
    event_count: int = 0
    byte_count: int = 0
    sent_count: int = 0
    truncated_count: int = 0
    started_at: float = field(default_factory=time.monotonic)
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    websocket: Any = None
    pending: list[StreamEvent] = field(default_factory=list)
    send_task: asyncio.Task | None = None
    max_events: int = 2000
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def finished(self) -> bool:
        """会话是否已经结束（用于前端判断是否继续轮询）。

        实现：``closed``/``error``/``cancelled`` 三种状态都算结束，
        ``open``/``connecting`` 表示仍在观察中。
        """
        return self.status in {"closed", "error", "cancelled"}

    def append(
        self,
        kind: str,
        data: str | None,
        *,
        direction: str = "in",
        event_name: str | None = None,
        raw_bytes: int | None = None,
    ) -> StreamEvent | None:
        """追加一条事件到内存时间线与待落库队列。

        作用：把“序号分配、大小记账、截断判定”集中在一处，
        保证内存视图与数据库行完全一致。
        实现：``sequence`` 自增；正文超过 ``MAX_EVENT_CHARS`` 时截断并计数；
        ``raw_bytes`` 用于 WebSocket 二进制帧（真实字节数可能大于可解码文本长度）。
        """
        if self.event_count >= self.max_events:
            self.finish("closed", "limits_exceeded")
            return None
        sequence = self.event_count + 1
        self.event_count = sequence
        size = raw_bytes if raw_bytes is not None else len((data or "").encode())
        truncated = len(data or "") > MAX_EVENT_CHARS
        event = StreamEvent(
            sequence=sequence,
            direction=direction,
            kind=kind,
            event_name=event_name,
            data=(data or "")[:MAX_EVENT_CHARS] if data is not None else None,
            data_bytes=size,
            truncated=truncated,
            elapsed_ms=int((time.monotonic() - self.started_at) * 1000),
        )
        if truncated:
            self.truncated_count += 1
        # Receive bytes are accounted by the transport, including partial input.
        self.events.append(event)
        self.pending.append(event)
        self.notify.set()
        return event

    def finish(self, status: Status, reason: str, *, error_kind: str | None = None):
        """把会话标记为终态并唤醒所有等待者。

        实现：``status`` 与 ``stop_reason`` 分开记录——前者是生命周期，
        后者是结束原因，二者组合才能表达“目标关闭 / 本服务主动断开 / 连接失败”。
        """
        if self.finished:
            return
        self.status = status
        self.stop_reason = reason
        if error_kind:
            self.error_kind = error_kind
        self.closed_at = utc_now()
        self.notify.set()


def serialize_event(event: StreamEvent, stream_id: str) -> dict:
    """把内存事件序列化成对外 JSON。

    实现：字段名与前端类型一一对应；``sequence`` 是增量拉取的游标，
    因此必须原样透出，不能重新编号。
    """
    return {
        "session_id": stream_id,
        "sequence": event.sequence,
        "direction": event.direction,
        "kind": event.kind,
        "event_name": event.event_name,
        "data": event.data,
        "data_bytes": event.data_bytes,
        "truncated": event.truncated,
        "elapsed_ms": event.elapsed_ms,
    }


def serialize_stream(stream: ActiveStream) -> dict:
    """把会话状态序列化成对外 JSON。

    实现：``finished`` 是派生值（终态判定），让前端不必自己枚举三种终态；
    时间戳统一走 ``as_utc`` 以补齐 SQLite 丢失的时区。
    """
    return {
        "id": stream.id,
        "kind": stream.kind,
        "url": stream.url,
        "status": stream.status,
        "finished": stream.finished,
        "stop_reason": stream.stop_reason,
        "error_kind": stream.error_kind,
        "error_message": stream.error_message,
        "http_status": stream.http_status,
        "content_type": stream.content_type,
        "subprotocol": stream.subprotocol,
        "close_code": stream.close_code,
        "close_reason": stream.close_reason,
        "event_count": stream.event_count,
        "byte_count": stream.byte_count,
        "sent_count": stream.sent_count,
        "truncated_count": stream.truncated_count,
        "opened_at": as_utc(stream.opened_at),
        "closed_at": as_utc(stream.closed_at),
    }
