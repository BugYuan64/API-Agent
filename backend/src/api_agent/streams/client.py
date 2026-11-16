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

    async def execute(self, payload: StreamStart):
        """Observe one bounded session; task cancellation always closes the owned connection."""
        stream = await self.manager.start(payload, self.allowed_origins)
        try:
            await asyncio.shield(stream.task)
        finally:
            if not stream.finished:
                await self.manager.stop(stream.id)
        return {
            "session": StreamSessionResponse.model_validate(serialize_stream(stream)).model_dump(
                mode="json"
            ),
            "events": [serialize_event(event, stream.id) for event in stream.events],
        }
