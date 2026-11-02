"""Bound transport reads before accumulating messages or decoding SSE lines."""

import codecs
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from websockets.asyncio.client import connect

if TYPE_CHECKING:
    import httpx

    from api_agent.streams.service import ActiveStream


class NoRedirectConnect(connect):
    """Keep the original handshake response without following its Location."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


class StreamLimitExceeded(Exception):
    """Incoming transport bytes exhausted the configured budget."""


async def bounded_sse_lines(
    response: "httpx.Response", stream: "ActiveStream", max_bytes: int
) -> AsyncIterator[str]:
    """Count raw identity bytes, including partial lines, before UTF-8 decoding.

    Total line and event buffers cannot exceed the session byte budget. Keep
    decoder state across chunks and treat CRLF, including split CRLF, as one EOL.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
    pending = ""
    skip_lf = False
    async for raw in response.aiter_raw():
        remaining = max_bytes - stream.byte_count
        chunk = raw[:remaining]
        stream.byte_count += len(chunk)
        text = decoder.decode(chunk)
        if text:
            if skip_lf and text.startswith("\n"):
                text = text[1:]
            skip_lf = text.endswith("\r")
            lines = re.split(r"\r\n|\r|\n", pending + text)
            pending = lines.pop()
            for line in lines:
                yield line
        if stream.byte_count >= max_bytes:
            raise StreamLimitExceeded
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending
