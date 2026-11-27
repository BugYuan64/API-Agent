"""Task-friendly WebSocket adapter over the interactive session manager."""

import asyncio

from api_agent.streams.schemas import StreamSessionResponse, StreamStart
from api_agent.streams.state import serialize_event, serialize_stream


class WebSocketTestClient:
    """Reuse the debugger's handshake, message scheduling, bounds and cleanup."""

    def __init__(self, manager, allowed_origins):
        """Bind the application-owned session manager without duplicating transports."""
        self.manager = manager
        self.allowed_origins = allowed_origins

    async def execute(
        self,
        payload: StreamStart,
        *,
        persist_history: bool = False,
        source: str | None = None,
        parent_session_id: str | None = None,
        round_index: int = 1,
        case_index: int = 0,
        case_label: str | None = None,
    ) -> dict:
        """Observe one bounded session; cancellation always closes the owned connection.

        默认 ``persist_history=False``，避免智能群测内部 WebSocket 探测污染顶层
        ``stream_sessions`` 历史表；分组 AI 套件可显式传入 ``persist_history=True``
        与 ``parent_session_id`` 将子用例归档到父套件之下。
        """
        effective_source = (
            source if source is not None else ("ai" if parent_session_id else "manual")
        )
        stream = await self.manager.start(
            payload,
            self.allowed_origins,
            persist_history=persist_history,
            source=effective_source,
            parent_session_id=parent_session_id,
            round_index=round_index,
            case_index=case_index,
            case_label=case_label,
        )
        try:
            await asyncio.shield(stream.task)
        finally:
            if not stream.finished or stream.id in self.manager.active:
                await self.manager.stop(stream.id)
        session_payload = {
            **serialize_stream(stream),
            "source": effective_source,
            "parent_session_id": parent_session_id,
            "round_index": round_index,
            "case_index": case_index,
            "case_label": case_label,
        }
        return {
            "session": StreamSessionResponse.model_validate(session_payload).model_dump(
                mode="json"
            ),
            "events": [serialize_event(event, stream.id) for event in stream.events],
        }
