"""Validated HTTP sends issued by the local console for authorized targets."""

import asyncio
import json
import time
from typing import Literal

import httpx
from pydantic import Field, field_validator, model_validator

from api_agent.console.sse import SseProgress
from api_agent.infrastructure.api import ApiInput
from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.http import (
    MAX_BODY_CHARS,
    ResponseEncodingError,
    ResponseLimitError,
    bounded_send,
    decode_text,
    group_headers,
)
from api_agent.infrastructure.outcomes import OutcomeFields, outcome_for, transport_error_kind
from api_agent.infrastructure.targets import (
    format_host_literal,
    validate_headers,
    validate_target,
)


class ConsoleRequest(ApiInput):
    """控制台一次手动请求的输入契约。

    作用：把请求做成一份可校验的数据，由本进程按用户意图发出，
    使界面能立刻看到目标真实回包（含响应头与响应体）。
    实现：请求体用字符串承载，``body_mode`` 决定它按 JSON、表单还是纯文本发送；
    ``valid_body`` 在接收阶段就检查“方法与请求体是否自洽”以及“JSON 是否可解析”，
    让语法错误立即暴露，而不是变成一个目标侧 400 才让人猜。
    """

    method: Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    body_mode: Literal["none", "json", "text", "form"] = "none"
    body: str | None = Field(default=None, max_length=MAX_BODY_CHARS)
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000)
    follow_redirects: bool = False
    sse_max_events: int = Field(default=50, ge=1, le=1000)
    sse_duration_ms: int = Field(default=3000, ge=100, le=30_000)

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """在字段级执行头策略，并保留用户书写的大小写。

        实现：用共享的 ``validate_headers`` 检查命名与保留头；
        返回原字典（不做小写化），发送时再统一小写去重，
        这样界面上看到的就是用户自己的写法。
        """
        validate_headers(value)
        return value

    @model_validator(mode="after")
    def valid_body(self):
        """校验请求体与方法的组合是否自洽。

        实现：无实体语义的方法不允许带请求体；选了 body 模式就必须有内容；
        ``json`` 模式要求能解析成 JSON（拒绝 ``NaN`` 之类非标准值由 ``json.loads`` 兜住）。
        """
        if self.method in {"GET", "HEAD", "OPTIONS"} and self.body_mode != "none":
            raise ValueError("this method does not support a request body")
        if self.body_mode != "none" and not (self.body or "").strip():
            raise ValueError("body_mode requires a non-empty body")
        if self.body_mode == "json":
            try:
                json.loads(self.body or "")
            except ValueError:
                raise ValueError("body_mode=json requires a valid JSON document") from None
        return self


class RedirectHop(ApiInput):
    """重定向链路的一跳：全部取自目标真实回包，不由本项目推断。

    作用：让界面能显示 ``301 → 302 → 200`` 的完整经过，而不是一串看不出结果的地址。
    实现：``status``/``reason`` 是这一跳响应自身的状态；``url`` 是它 ``Location``
    指向的下一跳请求地址；``cross_origin`` 记录跨源跳转——该跳发生时凭据头已被剥离，
    界面据此提示用户“跳转后不再携带鉴权”，与实际发送行为一致。
    """

    status: int
    reason: str | None = None
    url: str
    cross_origin: bool = False


class ConsoleResponse(ApiInput):
    """控制台的即时回包：原样保留，供本机界面核对。

    实现：``headers`` 聚合为“头名 → 值列表”以保留多值头（如多个 Set-Cookie）；
    ``body_bytes`` 是真实字节数，配合 ``body_truncated`` 让界面能区分
    “响应本来就短”和“被本服务截断”。
    """

    status: int
    reason: str | None = None
    http_version: str | None = None
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: str | None = None
    body_bytes: int = 0
    body_truncated: bool = False
    body_encoding: str | None = None
    elapsed_ms: float = 0
    redirects: list[RedirectHop] = Field(default_factory=list)
    sse: dict | None = None


class ConsoleResult(ApiInput, OutcomeFields):
    """一次控制台请求的结果：成功与失败走同一结构。

    实现：``ok`` 表示是否拿到了 HTTP 回包；连接失败时 ``error_kind`` 给出固定类别，
    ``response`` 为空——绝不把“连不上”伪装成某个 HTTP 状态码，
    否则界面会把传输故障误读成“目标返回了错误”。
    """

    ok: bool
    history_id: str | None = None
    request: dict
    response: ConsoleResponse | None = None
    error_kind: str | None = None
    error_message: str | None = None
    elapsed_ms: float = 0


def build_url(parsed) -> str:
    """用规范化后的 origin + 路径 + 查询串重建实际请求 URL（支持域名与 IPv4/IPv6）。

    实现：没有路径时补 ``/``；查询串原样保留（不重排序），
    避免把用户精心构造的参数顺序打乱。
    """
    literal = format_host_literal(parsed.hostname or "")
    netloc = literal + (f":{parsed.port}" if parsed.port is not None else "")
    return f"{parsed.scheme.lower()}://{netloc}{parsed.path or '/'}" + (
        f"?{parsed.query}" if parsed.query else ""
    )


async def _send(
    settings: Settings, payload: ConsoleRequest, on_sse: SseProgress | None
) -> ConsoleResult:
    """发出一次受控 HTTP 请求并返回结构化结果。

    作用：这是界面“发送”按钮的实现，也是本项目里第一条由用户界面触发真实请求的路径，
    因此范围裁定（回环 + 可选允许列表）、禁跳转、超时都在这里统一处置。
    实现：先 ``validate_target``；再按 ``body_mode`` 组装内容并补默认 Content-Type；
    用 ``httpx.AsyncClient`` 且默认 ``follow_redirects=False``——
    目标若返回 3xx，界面会看到 302 本身，而不会被静默带到授权范围之外；
    是否跟随系统代理受 ``settings.trust_system_proxy`` 控制。
    """
    parsed, _origin = validate_target(payload.url, settings.target_allowed_origins)
    url = build_url(parsed)
    headers = dict(payload.headers)
    headers["Accept-Encoding"] = "identity"
    content: bytes | None = None
    if payload.body_mode != "none":
        content = (payload.body or "").encode("utf-8")
        defaults = {
            "json": "application/json",
            "form": "application/x-www-form-urlencoded",
            "text": "text/plain; charset=utf-8",
        }
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = defaults[payload.body_mode]

    started = time.monotonic()
    request_summary = {"method": payload.method, "url": url, "at": None}
    sse_options = (payload.sse_max_events, min(payload.sse_duration_ms, payload.timeout_ms))
    try:
        # 所有跳转及完整正文共享挂钟预算，不能靠持续小片数据重置读超时。
        async with (
            asyncio.timeout(payload.timeout_ms / 1000),
            httpx.AsyncClient(
                timeout=payload.timeout_ms / 1000,
                follow_redirects=False,
                trust_env=settings.trust_system_proxy,
            ) as client,
        ):
            request = client.build_request(payload.method, url, headers=headers, content=content)
            response = await bounded_send(
                client,
                request,
                settings.response_max_bytes,
                sse_options=sse_options,
                on_sse=on_sse,
                follow_redirects=payload.follow_redirects,
            )
            history = []
            hops: list[RedirectHop] = []
            while payload.follow_redirects and response.next_request is not None:
                if len(history) >= client.max_redirects:
                    raise httpx.TooManyRedirects(
                        "Redirect limit exceeded", request=response.request
                    )
                next_request = response.next_request
                _parsed, next_origin = validate_target(
                    str(next_request.url), settings.target_allowed_origins
                )
                cross_origin = next_origin != _origin
                if cross_origin:
                    # Custom headers may carry credentials under arbitrary names.
                    for name in {
                        *payload.headers,
                        "authorization",
                        "proxy-authorization",
                        "cookie",
                    }:
                        next_request.headers.pop(name, None)
                    client.cookies.clear()
                history.append(response)
                # 记录这一跳的真实回包：状态、原因与它指向的下一跳地址。
                hops.append(
                    RedirectHop(
                        status=response.status_code,
                        reason=response.reason_phrase or None,
                        url=str(next_request.url),
                        cross_origin=cross_origin,
                    )
                )
                next_request.headers["Accept-Encoding"] = "identity"
                response = await bounded_send(
                    client,
                    next_request,
                    settings.response_max_bytes,
                    sse_options=sse_options,
                    on_sse=on_sse,
                    follow_redirects=True,
                )
                _origin = next_origin
            response.history = history
    except (ResponseLimitError, ResponseEncodingError) as error:
        return ConsoleResult(
            ok=False,
            request=request_summary,
            error_kind=(
                "response_limit_exceeded"
                if isinstance(error, ResponseLimitError)
                else "response_encoding_not_supported"
                if str(error) == "response_encoding_not_supported"
                else "response_decode_error"
            ),
            error_message=outcome_for(
                error_kind=(
                    "response_limit_exceeded"
                    if isinstance(error, ResponseLimitError)
                    else "response_decode_error"
                )
            ).message,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
    except (TimeoutError, httpx.TimeoutException):
        return ConsoleResult(
            ok=False,
            request=request_summary,
            error_kind="request_timeout",
            error_message=outcome_for(error_kind="request_timeout").message,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
    except (httpx.HTTPError, OSError) as error:
        return ConsoleResult(
            ok=False,
            request=request_summary,
            error_kind=transport_error_kind(error),
            error_message=safe_message(error),
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    raw = response.content
    text, truncated = decode_text(raw, MAX_BODY_CHARS)
    return ConsoleResult(
        ok=True,
        request=request_summary,
        response=ConsoleResponse(
            status=response.status_code,
            reason=response.reason_phrase or None,
            http_version=response.http_version,
            headers=group_headers(response.headers.multi_items()),
            body=text,
            body_bytes=len(raw),
            body_truncated=truncated
            or response.extensions.get("sse", {}).get("stop_reason")
            in {"byte_limit", "event_limit", "observation_complete"},
            body_encoding=response.encoding,
            elapsed_ms=elapsed,
            redirects=hops,
            sse=response.extensions.get("sse"),
        ),
        elapsed_ms=elapsed,
    )


def safe_message(error: Exception) -> str:
    """根据传输异常类型返回固定安全说明，不读取异常原文或本机连接细节。"""
    return outcome_for(error_kind=transport_error_kind(error)).message


class HttpTestClient:
    """Single HTTP execution class used by interactive requests and document tasks."""

    def __init__(self, settings: Settings):
        """Keep local target and response budgets; do not open connections."""
        self.settings = settings

    async def execute(
        self, payload: ConsoleRequest, *, on_sse: SseProgress | None = None
    ) -> ConsoleResult:
        """Return bounded transport evidence, optionally reporting SSE progress while reading."""
        return await _send(self.settings, payload, on_sse)


async def send(
    settings: Settings, payload: ConsoleRequest, *, on_sse: SseProgress | None = None
) -> ConsoleResult:
    """Compatibility entry for existing console callers, sharing the execution class."""
    return await HttpTestClient(settings).execute(payload, on_sse=on_sse)
