"""Bounded HTTP event-stream observation shared by console and case tasks."""

import asyncio
import codecs
import re

import httpx


async def observe_sse(response, max_bytes, max_events, duration_ms):
    """Collect complete SSE events within byte/event/time limits, preserving partial evidence."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
    raw_body = bytearray()
    pending = ""
    data = []
    name = "message"
    events = []
    reason = "target_closed"
    skip_lf = False

    def line_received(line):
        """Dispatch only blank-line-terminated data events, including multiline data."""
        nonlocal name, data
        if not line:
            if data:
                events.append({"event": name, "data": "\n".join(data)})
            data, name = [], "message"
        elif line.startswith("data:"):
            data.append(line[6:] if line[5:6] == " " else line[5:])
        elif line.startswith("event:"):
            name = line[6:].lstrip(" ") or "message"

    if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
        from api_agent.infrastructure.http import ResponseEncodingError

        raise ResponseEncodingError("response_encoding_not_supported")
    try:
        async with asyncio.timeout(duration_ms / 1000):
            async for chunk in response.aiter_raw():
                remaining = max_bytes - len(raw_body)
                raw_body.extend(chunk[:remaining])
                decoded = decoder.decode(chunk[:remaining])
                if skip_lf and decoded.startswith("\n"):
                    decoded = decoded[1:]
                if decoded:
                    skip_lf = decoded.endswith("\r")
                    lines = re.split(r"\r\n|\r|\n", pending + decoded)
                    pending = lines.pop()
                    for line in lines:
                        line_received(line)
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
    result.extensions["sse"] = {"events": events, "stop_reason": reason}
    return result
