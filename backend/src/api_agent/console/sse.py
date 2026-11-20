"""Bounded HTTP event-stream observation shared by console and case tasks."""

import asyncio
import codecs
import re
from collections.abc import Awaitable, Callable
from typing import TypedDict

import httpx


class SseEvent(TypedDict):
    event: str
    data: str


SseProgress = Callable[[SseEvent | None], Awaitable[None]]


async def observe_sse(
    response, max_bytes, max_events, duration_ms, on_sse: SseProgress | None = None
):
    """Collect bounded evidence and publish stream start (None) and complete SSE events."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
    raw_body = bytearray()
    pending = ""
    data = []
    name = "message"
    events: list[SseEvent] = []
    reason = "target_closed"
    skip_lf = False

    def line_received(line):
        """Dispatch only blank-line-terminated data events, including multiline data."""
        nonlocal name, data
        event = None
        if not line:
            if data:
                event = SseEvent(event=name, data="\n".join(data))
            data, name = [], "message"
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
            elif field == "event":
                name = value or "message"
        return event

    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        from api_agent.infrastructure.http import ResponseEncodingError

        raise ResponseEncodingError("response_encoding_not_supported")
    try:
        async with asyncio.timeout(duration_ms / 1000):
            if on_sse is not None:
                await on_sse(None)
            async for chunk in raw_chunks(response):
                remaining = max_bytes - len(raw_body)
                raw_body.extend(chunk[:remaining])
                decoded = decoder.decode(chunk[:remaining])
                if decoded:
                    if skip_lf:
                        decoded = decoded.removeprefix("\n")
                    skip_lf = decoded.endswith("\r")
                    lines = re.split(r"\r\n|\r|\n", pending + decoded)
                    pending = lines.pop()
                    for line in lines:
                        event = line_received(line)
                        if event is not None:
                            events.append(event)
                            if on_sse is not None:
                                await on_sse(event)
                        if len(events) >= max_events:
                            reason = "event_limit"
                            break
                if len(events) >= max_events:
                    break
                if len(raw_body) >= max_bytes:
                    reason = "byte_limit"
                    break
    except (TimeoutError, httpx.TimeoutException):
        reason = "observation_complete"
    result = httpx.Response(
        response.status_code,
        content=bytes(raw_body),
        request=response.request,
        extensions=response.extensions,
    )
    result.headers = response.headers.copy()
    result.next_request = response.next_request
    result.extensions["sse"] = {"events": events, "stop_reason": reason}
    return result


async def raw_chunks(response: httpx.Response):
    """Read upstream chunks as they arrive, including preloaded fixture responses."""
    if response.is_stream_consumed:
        yield response.content
    else:
        async for chunk in response.aiter_raw():
            yield chunk
