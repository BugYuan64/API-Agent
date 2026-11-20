"""Bounded response reading plus shared header/body helpers.

作用：一是把“先缓冲再检查”改成“边读边记账”，避免超大或压缩响应在预算生效前
耗尽管理进程内存；二是集中多值头聚合与正文解码，这两件事此前在控制台、
native 引擎与 Postman 报告归一化中各有一份实现。
"""

import zlib
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from api_agent.console.sse import SseProgress

MAX_BODY_CHARS = 1_000_000


class ResponseLimitError(ValueError):
    """响应原始或解码字节超过预算；调用方保留未知断言，不伪造目标状态码。"""

    pass


class ResponseEncodingError(ValueError):
    """响应编码不支持或解压不完整，表示传输不可评估而非已确认业务失败。"""

    pass


def group_headers(items) -> dict[str, list[str]]:
    """把 ``multi_items()`` 聚合成“头名 → 值列表”，保留目标真实写法与全部值。

    实现：同名头按出现顺序追加；键名不做大小写归一，
    让用户看到目标究竟是怎么写的（如 ``Set-Cookie`` 出现两次）。
    """
    grouped: dict[str, list[str]] = {}
    for name, value in items:
        grouped.setdefault(name, []).append(value)
    return grouped


def decode_text(raw: bytes, limit: int = MAX_BODY_CHARS) -> tuple[str | None, bool]:
    """把响应字节解码成文本并截断，返回 ``(文本, 是否被截断)``。

    实现：优先严格 UTF-8，失败退回替换模式（二进制响应会显示 ``�``），
    保证界面不会因为一个非文本响应整块失效；
    截断判定与截断动作在同一次调用里完成，避免调用方各自重算
    （此前用 ``len(text) >= limit`` 还把恰好等于上限的正文误标成截断）。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    if len(text) > limit:
        return text[:limit], True
    return text, False


async def bounded_send(
    client: httpx.AsyncClient,
    request: httpx.Request,
    max_bytes: int,
    *,
    sse_options: tuple[int, int] | None = None,
    on_sse: "SseProgress | None" = None,
    follow_redirects: bool = False,
) -> httpx.Response:
    """发送已构造请求，读取与解压时检查字节预算并始终关闭原始流。返回有界缓冲响应；不自动重定向，超限和编码失败抛专门异常。"""
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if sse_options and media_type == "text/event-stream":
            from api_agent.console.sse import observe_sse

            publish = None if follow_redirects and response.next_request is not None else on_sse
            return await observe_sse(response, max_bytes, *sse_options, publish)
        # In-memory transports may supply an already-consumed response.
        if response.is_stream_consumed:
            if len(response.content) > max_bytes:
                raise ResponseLimitError("response_limit_exceeded")
            return response
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"", "identity", "gzip", "deflate"}:
            raise ResponseEncodingError("response_encoding_not_supported")
        decoder = None
        if encoding in {"gzip", "deflate"}:
            decoder = zlib.decompressobj(
                16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
            )
        content = bytearray()
        raw_count = 0
        async for raw in response.aiter_raw(chunk_size=min(64 * 1024, max_bytes + 1)):
            raw_count += len(raw)
            if raw_count > max_bytes:
                raise ResponseLimitError("response_limit_exceeded")
            if decoder is not None:
                try:
                    decoded = decoder.decompress(raw, max_bytes - len(content) + 1)
                except zlib.error:
                    raise ResponseEncodingError("response_decode_error") from None
                if decoder.unused_data:
                    # Do not silently ignore additional members/encoding layers.
                    raise ResponseEncodingError("response_encoding_not_supported")
            else:
                decoded = raw
            if len(content) + len(decoded) > max_bytes:
                raise ResponseLimitError("response_limit_exceeded")
            content.extend(decoded)
        if decoder is not None and not decoder.eof:
            raise ResponseEncodingError("response_decode_error")
        # Avoid HTTPX transparently decoding an already-decoded buffer a second time.
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in {"content-encoding", "content-length"}
        ]
        result = httpx.Response(
            response.status_code,
            headers=headers,
            content=bytes(content),
            request=response.request,
            extensions=response.extensions,
        )
        result.headers = response.headers.copy()
        result.next_request = response.next_request
        return result
    finally:
        await response.aclose()
