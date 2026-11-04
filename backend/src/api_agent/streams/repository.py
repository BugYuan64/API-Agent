"""Transactional stream evidence writes; acknowledge events only after commit."""

import logging

from sqlalchemy import update

from api_agent.infrastructure.database import Database
from api_agent.infrastructure.orm import utc_now
from api_agent.infrastructure.targets import redact_headers
from api_agent.streams.models import StreamEventRecord, StreamSessionRecord
from api_agent.streams.schemas import StreamStart
from api_agent.streams.state import ActiveStream

logger = logging.getLogger("api_agent")


class StreamRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create(self, stream: ActiveStream, payload: StreamStart, origin: str) -> None:
        async with self.database.session() as session:
            session.add(
                StreamSessionRecord(
                    id=stream.id,
                    project_id=payload.project_id,
                    kind=payload.kind,
                    url=stream.url,
                    request_headers=redact_headers(payload.headers),
                    status=stream.status,
                    settings={
                        "origin": origin,
                        "max_events": payload.max_events,
                        "max_duration_ms": payload.max_duration_ms,
                        "connect_timeout_ms": payload.connect_timeout_ms,
                        "ping_interval_ms": payload.ping_interval_ms,
                        "subprotocols": payload.subprotocols,
                        "scheduled_sends": len(payload.send),
                    },
                )
            )
            await session.commit()

    async def recover(self) -> int:
        async with self.database.session() as session:
            result = await session.execute(
                update(StreamSessionRecord)
                .where(StreamSessionRecord.status.in_(["connecting", "open"]))
                .values(status="closed", stop_reason="recovered_after_restart", closed_at=utc_now())
            )
            await session.commit()
            return result.rowcount

    async def flush(self, stream: ActiveStream, *, final: bool = False) -> None:
        # Serialize commits, not event capture. A send/receive can append while
        # SQLite awaits IO; only the committed prefix may be removed afterward.
        async with stream.flush_lock:
            if not stream.pending and not final:
                return
            pending = list(stream.pending)
            values = {
                name: getattr(stream, name)
                for name in (
                    "status",
                    "stop_reason",
                    "error_kind",
                    "error_message",
                    "http_status",
                    "content_type",
                    "subprotocol",
                    "close_code",
                    "close_reason",
                    "event_count",
                    "byte_count",
                    "sent_count",
                    "truncated_count",
                    "opened_at",
                    "closed_at",
                )
            }
            try:
                async with self.database.session() as session:
                    session.add_all(
                        [
                            StreamEventRecord(
                                session_id=stream.id,
                                sequence=event.sequence,
                                direction=event.direction,
                                kind=event.kind,
                                event_name=event.event_name,
                                data=event.data,
                                data_bytes=event.data_bytes,
                                truncated=event.truncated,
                                elapsed_ms=event.elapsed_ms,
                            )
                            for event in pending
                        ]
                    )
                    await session.execute(
                        update(StreamSessionRecord)
                        .where(StreamSessionRecord.id == stream.id)
                        .values(**values)
                    )
                    await session.commit()
            except Exception:  # A transient failure leaves the bounded queue for retry.
                logger.warning("Stream evidence could not be persisted", exc_info=True)
                return
            del stream.pending[: len(pending)]
