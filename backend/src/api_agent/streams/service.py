"""Own bounded SSE/WebSocket probes and coordinate their lifecycle and evidence."""

import asyncio
import base64

from api_agent.infrastructure.orm import new_id, utc_now
from api_agent.streams.repository import StreamRepository
from api_agent.streams.schemas import StreamStart
from api_agent.streams.state import (
    MAX_EVENT_CHARS,
    ActiveStream,
    Kind,
    StreamEvent,
    serialize_event,
    serialize_stream,
)
from api_agent.streams.transport import (
    NoRedirectConnect,
    StreamLimitExceeded,
    bounded_sse_lines,
)


class StreamManager:
    """管理本进程内的长连接会话：启动、停止、增量读取与落库。

    作用：SSE/WebSocket 是长时间运行的异步任务，不能挂在单个 HTTP 请求上；
    本类持有它们并把事件暴露成可轮询的时间线，使界面刷新后仍可回看。
    实现：``active`` 保存未结束会话，``history`` 保存已结束但仍在内存中的会话
    （供刚结束时的快速回看），超过容量的历史会话只从数据库读取。
    """

    def __init__(self, database, settings):
        """保存数据库与配置，并初始化会话表。

        实现：启动名额在第一次 await 前预留；上限与时长来自配置。
        """
        self.database = database
        self.repository = StreamRepository(database)
        self.settings = settings
        self.active: dict[str, ActiveStream] = {}
        self.history: dict[str, ActiveStream] = {}
        self.max_history = 20

    async def start(self, payload: StreamStart, allowed_origins: list[str]) -> ActiveStream:
        """校验目标后启动一个长连接会话。

        作用：这是唯一的启动入口，范围裁定（回环 + 可选允许列表）在此生效。
        实现：先限制并发会话数（避免界面误开大量连接把本机端口耗尽），
        再预写下会话行（状态 connecting），最后把真正的连接过程交给后台任务——
        这样接口能立刻返回会话 ID，界面无需等待握手完成。
        """
        if len(self.active) >= self.settings.stream_max_sessions:
            raise ValueError("stream_session_limit")
        from api_agent.infrastructure.targets import validate_target

        schemes = frozenset({"http", "https", "ws", "wss"})
        parsed, origin = validate_target(payload.url, allowed_origins, schemes)
        url = _canonical_stream_url(parsed, payload.kind)
        stream = ActiveStream(
            id=new_id(), kind=payload.kind, url=url, max_events=payload.max_events
        )
        self.active[stream.id] = stream
        try:
            await self._persist_new(stream, payload, origin)
            stream.task = asyncio.create_task(
                self._run(stream, payload, url), name=f"stream-{stream.kind}-{stream.id[:8]}"
            )
        except BaseException:
            self.active.pop(stream.id, None)
            raise
        return stream

    async def stop(self, session_id: str, *, reason: str = "client_stopped") -> ActiveStream | None:
        """结束一个会话并等待收尾完成。

        作用：调用方拿到的返回值应当是终态，否则界面会显示“已停止”但状态仍是 open。
        实现：设置 ``stop_reason="client_stopped"`` 后取消任务，再 ``await`` 该任务——
        任务的 ``finally`` 负责落库与状态归一，因此等到它结束就说明证据已经写完。
        已结束的会话直接返回，重复请求幂等。等待设 5 秒上限，
        避免目标侧不响应时把 HTTP 请求一直挂住。
        """
        stream = self.active.get(session_id) or self.history.get(session_id)
        if stream is None:
            return None
        if stream.finished and stream.id not in self.active:
            return stream
        was_finished = stream.finished
        if not was_finished:
            stream.stop_reason = reason
        task = stream.task
        if task is not None and not task.done():
            # A terminal task may already be committing its last evidence.
            # Cancelling that finally block would interrupt the owned teardown.
            if not was_finished:
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001 - 收尾异常已由任务自身记录
                pass
        # A task cancelled before its first instruction never enters _run's
        # try/finally. The reserved slot and persisted row still need retiring.
        if task is not None and task.done() and stream.id in self.active:
            if not stream.finished:
                stream.finish("cancelled", reason)
            await self._flush(stream, final=True)
            self.active.pop(stream.id, None)
            self.history[stream.id] = stream
            self._trim_history()
        return stream

    async def shutdown(self) -> None:
        """应用关闭时回收所有在跑的会话。

        实现：先请求停止再逐个 ``await`` 任务（吞掉取消异常），
        确保没有任务在事件循环关闭后还持有连接。
        """
        await asyncio.gather(
            *(
                self.stop(stream.id, reason="service_shutdown")
                for stream in list(self.active.values())
            )
        )
        # stop() bounds an HTTP caller's wait. Application shutdown must also
        # join tasks still finalizing after that grace period, before DB disposal.
        await asyncio.gather(
            *(stream.task for stream in list(self.active.values()) if stream.task is not None),
            return_exceptions=True,
        )

    async def recover(self) -> int:
        """Close persisted connections lost with the previous process."""
        return await self.repository.recover()

    def find(self, session_id: str) -> ActiveStream | None:
        """按 ID 在活跃与近期历史中查找会话。

        实现：先查 ``active`` 再查 ``history``；都不在时返回 None，
        由调用方决定回落数据库还是返回 404。
        """
        return self.active.get(session_id) or self.history.get(session_id)

    async def send_text(self, session_id: str, message: str) -> int:
        """向一个 WebSocket 会话发送文本消息。

        作用：长连接测试里“发一条再收一条”是常见验证手法，
        没有这个能力就只能测只读流。
        实现：仅 WebSocket 且状态为 open 时可发；消息长度上限 64KiB；
        发送成功后本地也记一条 ``direction="out"`` 的事件，便于时间线对齐。
        """
        stream = self.find(session_id)
        if stream is None:
            raise ValueError("stream_not_found")
        if stream.kind != "websocket":
            raise ValueError("stream_not_sendable")
        if len(message) > MAX_EVENT_CHARS:
            raise ValueError("stream_message_too_large")
        await self._send_text(stream, message)
        return stream.event_count

    async def _send_text(self, stream: ActiveStream, message: str) -> None:
        """发送一条文本消息，并在发送前与接收侧共享同一份事件预算。

        实现：先取锁、检查状态与剩余额度，再 ``send`` 并本地记账，
        这样“发送”与“收到回显”不会各自越过上限。
        ``connection.close()`` 必须在**释放锁之后**执行——关闭握手需要
        等待接收协程退出，而接收协程可能正等这把锁，持锁关闭会互相卡死。
        """
        connection = None
        limit_hit = False
        async with stream.event_lock:
            if stream.status != "open" or stream.websocket is None:
                raise ValueError("stream_not_open")
            if stream.event_count >= stream.max_events:
                # 已在终态或额度耗尽：不再排队等待传输层，直接判冲突。
                stream.finish("closed", "limits_exceeded")
                raise ValueError("stream_event_limit")
            # 持锁跨越 send，先占用本条目名额并记录出站证据，
            # 避免回显抢在同一窗口内把接收事件插到前面。
            connection = stream.websocket
            if getattr(getattr(connection, "protocol", None), "state", None) is not None:
                # websockets 15 的 Protocol.state 在关闭流程开始后即为 CLOSING；
                # 此时 send() 会挂在写队列上长期不返回（Windows 上尤其明显），
                # 因此必须先判状态，再决定是否发送。
                try:
                    from websockets.protocol import State

                    if connection.protocol.state is not State.OPEN:
                        raise ValueError("stream_not_open")
                except ImportError:  # pragma: no cover - 旧版本无 State 枚举
                    pass
            try:
                await connection.send(message)
            except Exception as error:  # noqa: BLE001
                from websockets.exceptions import ConnectionClosed

                if isinstance(error, (ConnectionClosed, OSError)):
                    # 发送失败不占用事件额度，交给读循环记录真实关闭原因。
                    raise ValueError("stream_not_open") from None
                raise
            stream.sent_count += 1
            stream.append("text", message, direction="out")
            limit_hit = stream.event_count >= stream.max_events
            if limit_hit:
                stream.finish("closed", "limits_exceeded")
        await self._flush(stream)
        if limit_hit and connection is not None:
            # 锁已释放：此时关闭不会与接收协程争锁。
            # 关闭握手可能等对端回应，因此不阻塞调用方——发送路径只负责
            # 发起断开；真正的收尾与落库由读循环的 finally 完成。
            self._close_connection_soon(stream, connection)

    async def read(
        self, session_id: str, after: int, wait_ms: int = 0
    ) -> tuple[ActiveStream, list, bool]:
        """增量读取时间线，可短暂等待新事件（长轮询）。

        作用：让界面用 200ms～2s 的轮询就获得接近实时的体验，
        比再开一条浏览器 WebSocket 更简单，也不会突破本机的连接预算。
        实现：先取 ``sequence > after`` 的事件；若为空且会话未结束，
        最多等 ``wait_ms`` 让 ``notify`` 置位后重取一次——只在有事件或结束时返回，
        避免无意义的空轮询占满请求。
        """
        stream = self.find(session_id)
        if stream is None:
            return None, [], False
        events = [event for event in stream.events if event.sequence > after]
        if not events and not stream.finished and wait_ms > 0:
            stream.notify.clear()
            try:
                await asyncio.wait_for(stream.notify.wait(), timeout=wait_ms / 1000)
            except TimeoutError:
                pass
            events = [event for event in stream.events if event.sequence > after]
        return stream, events, stream.finished

    async def _run(self, stream: ActiveStream, payload: StreamStart, url: str) -> None:
        """会话主体：按类型连接、读事件、按上限收尾并落库。

        实现：把 SSE 与 WebSocket 分派到各自实现；
        用独立任务在 ``max_duration_ms`` 后取消本任务，实现总时长上限；
        ``finally`` 统一做收尾——结束状态、落库、移入历史，
        保证无论成功、异常还是被取消，证据都不会丢。
        """
        limit = None
        try:
            limit = asyncio.create_task(
                _cancel_after(stream, payload.max_duration_ms / 1000),
                name=f"stream-limit-{stream.id[:8]}",
            )
            if payload.kind == "sse":
                await self._run_sse(stream, payload, url)
            else:
                await self._run_websocket(stream, payload, url)
        except StreamLimitExceeded:
            stream.finish("closed", "limits_exceeded")
        except asyncio.CancelledError:
            if stream.stop_reason == "client_stopped":
                stream.finish("cancelled", "client_stopped")
            elif stream.stop_reason == "deadline_exceeded":
                stream.finish("closed", "deadline_exceeded")
            elif stream.stop_reason == "service_shutdown":
                stream.finish("cancelled", "service_shutdown")
            else:
                stream.finish("cancelled", "cancelled")
        except Exception as error:  # noqa: BLE001 - 会话异常必须落成证据而不是 500
            stream.error_message = str(error)[:300] or type(error).__name__
            stream.error_kind = stream.error_kind or "stream_failed"
            stream.finish(
                "error", stream.stop_reason or "stream_failed", error_kind=stream.error_kind
            )
        finally:
            if limit is not None:
                limit.cancel()
            stream.websocket = None
            stream.notify.set()
            await self._flush(stream, final=True)
            self.active.pop(stream.id, None)
            self.history[stream.id] = stream
            self._trim_history()

    async def _run_sse(self, stream: ActiveStream, payload: StreamStart, url: str) -> None:
        """读取一次 SSE 流并逐事件记录。

        实现：用 ``httpx`` 流式 GET，``read=None`` 表示允许长时间静默
        （由总时长上限兜底）；首包到达后读取状态码与 Content-Type：
        非 2xx 判为握手被拒，Content-Type 不是 ``text/event-stream`` 仍继续尝试解析
        （部分服务端不设该头），但把实际类型记进会话，便于用户判断是否选错。
        逐行解析符合 WHATWG 的 SSE 语法：``data:``/``event:``/``id:``/``retry:``
        与 ``:`` 注释，空行派发事件；多行 ``data`` 用换行拼接。
        """
        import httpx

        headers = dict(payload.headers)
        # Automatic decompression would allocate before the byte-budget check.
        headers = {
            name: value for name, value in headers.items() if name.lower() != "accept-encoding"
        }
        headers["Accept-Encoding"] = "identity"
        if not any(name.lower() == "accept" for name in headers):
            headers["Accept"] = "text/event-stream"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=payload.connect_timeout_ms / 1000,
                read=None,
                write=payload.connect_timeout_ms / 1000,
                pool=payload.connect_timeout_ms / 1000,
            ),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                stream.http_status = response.status_code
                stream.content_type = response.headers.get("content-type")
                if not 200 <= response.status_code < 300:
                    stream.append(
                        "error",
                        f"Target rejected the stream with HTTP {response.status_code}",
                    )
                    stream.error_kind = "handshake_rejected"
                    stream.finish("error", "handshake_rejected")
                    return
                if (
                    response.headers.get("content-encoding", "identity").lower().strip()
                    != "identity"
                ):
                    stream.error_kind = "unsupported_content_encoding"
                    stream.finish("error", "unsupported_content_encoding")
                    return
                stream.status = "open"
                stream.opened_at = utc_now()
                stream.append("open", f"HTTP {response.status_code}")
                self._notify_status(stream)
                if stream.event_count >= payload.max_events:
                    stream.finish("closed", "limits_exceeded")
                    return
                data_lines: list[str] = []
                event_name: str | None = None
                async for line in bounded_sse_lines(
                    response, stream, self.settings.stream_max_bytes
                ):
                    if line == "":
                        if data_lines or event_name:
                            stream.append(
                                "message",
                                "\n".join(data_lines),
                                event_name=event_name,
                            )
                            data_lines, event_name = [], None
                        if stream.event_count >= payload.max_events:
                            stream.finish("closed", "limits_exceeded")
                            return
                        await self._flush(stream)
                        continue
                    if line.startswith(":"):
                        stream.append("comment", line[1:].lstrip())
                    elif line.startswith("data:"):
                        data_lines.append(line[6:] if line[5:6] == " " else line[5:])
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("id:") or line.startswith("retry:"):
                        continue
                    else:
                        # 非标准续行：按原文记录，避免静默丢数据。
                        stream.append("raw", line)
                    if stream.event_count >= payload.max_events:
                        stream.finish("closed", "limits_exceeded")
                        return
                stream.append("close", "Target ended the stream")
                stream.finish("closed", "target_closed")

    async def _run_websocket(self, stream: ActiveStream, payload: StreamStart, url: str) -> None:
        """建立 WebSocket 连接并逐帧记录。

        实现：用 ``websockets`` 客户端；握手成功的判定来自连接对象本身，
        服务端拒绝（如 401/404）会抛 ``InvalidStatus``，这里读其响应状态码记为
        ``handshake_rejected``；握手成功后置 open 并记录协商到的子协议。
        按消息分片记接收字节；整体期限与客户端心跳管理等待，
        不取消半条消息后重新 recv；达到字节上限立即关闭。
        二进制帧按 base64 记录（保证文本可读且可还原）。
        """
        from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI, PayloadTooBig

        headers = dict(payload.headers)
        try:
            connection = await NoRedirectConnect(
                url,
                additional_headers=headers,
                subprotocols=payload.subprotocols or None,
                open_timeout=payload.connect_timeout_ms / 1000,
                ping_interval=payload.ping_interval_ms / 1000 or None,
                ping_timeout=payload.ping_interval_ms / 1000 or 20,
                close_timeout=3,
                max_size=self.settings.stream_max_bytes,
                max_queue=1,
                proxy=None,
                compression="deflate",
            )
        except InvalidStatus as error:
            stream.http_status = getattr(error.response, "status_code", None)
            stream.append("error", f"Handshake rejected with HTTP {stream.http_status}")
            stream.error_kind = "handshake_rejected"
            stream.finish("error", "handshake_rejected")
            return
        except (InvalidURI, ValueError) as error:
            stream.error_kind = "invalid_url"
            stream.error_message = str(error)[:300]
            stream.finish("error", "connect_failed", error_kind="invalid_url")
            return
        except TimeoutError:
            stream.error_kind = "connect_timeout"
            stream.error_message = "The WebSocket handshake did not complete before the timeout"
            stream.finish("error", "connect_timeout", error_kind="connect_timeout")
            return
        except Exception as error:  # noqa: BLE001 - 连接失败必须落成证据
            stream.error_kind = "connect_failed"
            stream.error_message = str(error)[:300] or type(error).__name__
            stream.finish("error", "connect_failed", error_kind="connect_failed")
            return

        stream.websocket = connection
        stream.status = "open"
        stream.opened_at = utc_now()
        stream.subprotocol = connection.subprotocol
        stream.append(
            "open",
            "WebSocket handshake complete"
            + (f" (subprotocol={connection.subprotocol})" if connection.subprotocol else ""),
        )
        self._notify_status(stream)
        if self._limits_hit(stream, payload):
            stream.finish("closed", "limits_exceeded")
            await connection.close()
            return
        if payload.send:
            stream.send_task = asyncio.create_task(
                self._send_scheduled(stream, payload),
                name=f"stream-send-{stream.id[:8]}",
            )
        try:
            while True:
                # Do not cancel a partially consumed message and then resume recv.
                # The session deadline and the library's keepalive own timeouts.
                buffer = bytearray()
                binary = None
                async for fragment in connection.recv_streaming():
                    if binary is None:
                        binary = isinstance(fragment, bytes)
                    raw = fragment if isinstance(fragment, bytes) else fragment.encode()
                    size = len(raw)
                    remaining = self.settings.stream_max_bytes - stream.byte_count
                    stream.byte_count += min(size, remaining)
                    if size > remaining:
                        raise StreamLimitExceeded
                    buffer.extend(raw)
                if binary is None:
                    continue
                message = bytes(buffer) if binary else buffer.decode()
                async with stream.event_lock:
                    if stream.finished:
                        return
                    if isinstance(message, bytes):
                        stream.append(
                            "binary",
                            base64.b64encode(message).decode("ascii"),
                            raw_bytes=len(message),
                        )
                    else:
                        stream.append("text", message)
                    if self._limits_hit(stream, payload):
                        stream.finish("closed", "limits_exceeded")
                        return
                await self._flush(stream)
                connection.protocol.max_size = self.settings.stream_max_bytes - stream.byte_count
        except ConnectionClosed as error:
            if isinstance(connection.protocol.parser_exc, PayloadTooBig):
                stream.finish("closed", "limits_exceeded")
                return
            # 关闭帧的码与原因挂在 ``rcvd`` 上；``.code``/``.reason`` 在 websockets 15 已弃用。
            received = error.rcvd
            stream.close_code = received.code if received is not None else None
            stream.close_reason = ((received.reason if received is not None else "") or "")[
                :300
            ] or None
            if stream.close_code is None:
                label = "no close frame"
            elif stream.close_code == 1000:
                label = "normal"
            else:
                label = f"code {stream.close_code}"
            async with stream.event_lock:
                if not stream.finished:
                    stream.append("close", f"Target closed the connection ({label})")
                    stream.finish("closed", "target_closed")
        finally:
            if stream.send_task is not None and not stream.send_task.done():
                stream.send_task.cancel()
            try:
                await connection.close()
            except Exception:  # noqa: BLE001 - 关闭阶段的异常不影响证据
                pass

    async def _send_scheduled(self, stream: ActiveStream, payload: StreamStart) -> None:
        """按设定间隔把预置消息依次发给目标。

        实现：首条立即发（便于立刻验证“发一条收一条”），后续按 ``send_interval_ms`` 间隔；
        目标已关闭或会话结束时提前退出。
        当目标在我们等待间隔期间就关闭了连接，``websocket.send`` 会抛 ``ConnectionClosed``；
        这种情况是**预期内的收尾**，不该被当成用户可见的错误事件——
        否则时间线会多出一条「Send failed: received 1000」噪声，掩盖真正的关闭原因。
        只有非关闭类的发送异常才记录。
        """
        try:
            for index, message in enumerate(payload.send):
                if index and payload.send_interval_ms:
                    await asyncio.sleep(payload.send_interval_ms / 1000)
                if stream.status != "open" or stream.websocket is None:
                    return
                try:
                    await self._send_text(stream, message)
                except Exception as error:
                    # 连接已关闭（ConnectionClosed 及其子类）：静默退出，收尾由读循环记录。
                    from websockets.exceptions import ConnectionClosed

                    if isinstance(error, ConnectionClosed):
                        return
                    if isinstance(error, ValueError) and str(error) in {
                        "stream_not_open",
                        "stream_event_limit",
                    }:
                        return
                    stream.append("error", f"Send failed: {str(error)[:200]}")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 其余异常只记录，不掩盖读循环
            stream.append("error", f"Send failed: {str(error)[:200]}")

    def _limits_hit(self, stream: ActiveStream, payload: StreamStart) -> bool:
        """判断是否触发事件数或字节数上限。

        实现：任一上限达到即返回 True，由调用方以 ``limits_exceeded`` 收尾——
        与“目标主动关闭”区分开，避免把本服务的主动停止误报成目标行为。
        """
        return (
            stream.event_count >= payload.max_events
            or stream.byte_count >= self.settings.stream_max_bytes
        )

    def _notify_status(self, stream: ActiveStream) -> None:
        """唤醒轮询等待者（状态变化时无需等待超时即可返回）。

        实现：直接置位 ``notify``；等待方在 ``read`` 中先 clear 再 wait，
        因此不会丢失这次信号。
        """
        stream.notify.set()

    def _close_connection_soon(self, stream: ActiveStream, connection) -> None:
        """在不阻塞调用方的前提下发起关闭。

        作用：额度耗尽时要主动断开，但关闭握手可能等待对端回应；
        让发送 API 为一次关闭挂起数秒会拖慢界面并放大并发争用。
        实现：起一个短生命周期任务去关闭，异常吞掉——
        会话终态与证据已经由 ``finish`` 记录，关闭失败不改写结论。
        """

        async def close() -> None:
            try:
                await asyncio.wait_for(connection.close(), timeout=5)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        task = asyncio.create_task(close(), name=f"stream-close-{stream.id[:8]}")
        stream.send_task = stream.send_task or task

    def _trim_history(self) -> None:
        """限制内存中保留的历史会话数量。

        实现：按结束时间新到旧保留 ``max_history`` 个，
        更早的会话仍可从数据库读取，因此丢弃内存副本不影响可用性。
        """
        while len(self.history) > self.max_history:
            oldest = next(iter(self.history))
            self.history.pop(oldest, None)

    async def _persist_new(self, stream: ActiveStream, payload: StreamStart, origin: str) -> None:
        await self.repository.create(stream, payload, origin)

    async def _flush(self, stream: ActiveStream, *, final: bool = False) -> None:
        await self.repository.flush(stream, final=final)


async def _cancel_after(stream: ActiveStream, seconds: float) -> None:
    """在总时长到达时取消读循环，实现时间上限。

    实现：先 ``sleep`` 再判断是否仍在跑；设置 ``stop_reason`` 后取消任务，
    使 ``_run`` 的取消分支能区分“超时”和“用户停止”。
    """
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    if stream.task is not None and not stream.task.done():
        stream.stop_reason = "deadline_exceeded"
        stream.task.cancel()


def _canonical_stream_url(parsed, kind: Kind) -> str:
    """把用户输入规范成实际请求的 URL（协议按 kind 归一）。

    实现：SSE 一律走 http(s)；WebSocket 一律走 ws(s)——
    用户填 ``http://127.0.0.1:8080/events`` 也能测 SSE，
    填 ``ws://`` 或 ``http://`` 都能测 WebSocket，少一个必踩的坑。
    """
    from ipaddress import ip_address

    address = ip_address(parsed.hostname or "")
    literal = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = literal + (f":{parsed.port}" if parsed.port is not None else "")
    scheme = parsed.scheme
    if kind == "sse":
        scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    else:
        scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
    return f"{scheme}://{netloc}{parsed.path or '/'}" + (f"?{parsed.query}" if parsed.query else "")


__all__ = [
    "StreamEvent",
    "ActiveStream",
    "StreamManager",
    "StreamStart",
    "serialize_event",
    "serialize_stream",
]
