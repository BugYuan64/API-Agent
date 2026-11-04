"""Validated HTTP sends issued by the local console (loopback targets only)."""

import json
import time
from typing import Literal

import httpx
from pydantic import Field, field_validator, model_validator

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
from api_agent.infrastructure.targets import validate_headers, validate_target


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
    redirects: list[str] = Field(default_factory=list)


class ConsoleResult(ApiInput):
    """一次控制台请求的结果：成功与失败走同一结构。

    实现：``ok`` 表示是否拿到了 HTTP 回包；连接失败时 ``error_kind`` 给出固定类别，
    ``response`` 为空——绝不把“连不上”伪装成某个 HTTP 状态码，
    否则界面会把传输故障误读成“目标返回了错误”。
    """

    ok: bool
    request: dict
    response: ConsoleResponse | None = None
    error_kind: str | None = None
    error_message: str | None = None
    elapsed_ms: float = 0


def build_url(parsed) -> str:
    """用规范化后的 origin + 路径 + 查询串重建实际请求 URL。

    实现：没有路径时补 ``/``；查询串原样保留（不重排序），
    避免把用户精心构造的参数顺序打乱。
    """
    host = parsed.hostname or ""
    from ipaddress import ip_address

    address = ip_address(host)
    literal = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = literal + (f":{parsed.port}" if parsed.port is not None else "")
    return f"{parsed.scheme}://{netloc}{parsed.path or '/'}" + (
        f"?{parsed.query}" if parsed.query else ""
    )


async def send(settings: Settings, payload: ConsoleRequest) -> ConsoleResult:
    """发出一次受控 HTTP 请求并返回结构化结果。

    作用：这是界面“发送”按钮的实现，也是本项目里第一条由用户界面触发真实请求的路径，
    因此范围裁定（回环 + 可选允许列表）、禁跳转、超时都在这里统一处置。
    实现：先 ``validate_target``；再按 ``body_mode`` 组装内容并补默认 Content-Type；
    用 ``httpx.AsyncClient`` 且默认 ``follow_redirects=False``——
    目标若返回 3xx，界面会看到 302 本身，而不会被静默带到授权范围之外；
    ``trust_env=False`` 保证不读取系统代理配置。
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
    try:
        async with httpx.AsyncClient(
            timeout=payload.timeout_ms / 1000,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            request = client.build_request(payload.method, url, headers=headers, content=content)
            response = await bounded_send(client, request, settings.response_max_bytes)
            history = []
            while payload.follow_redirects and response.next_request is not None:
                if len(history) >= client.max_redirects:
                    raise httpx.TooManyRedirects(
                        "Redirect limit exceeded", request=response.request
                    )
                next_request = response.next_request
                _parsed, next_origin = validate_target(
                    str(next_request.url), settings.target_allowed_origins
                )
                if next_origin != _origin:
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
                next_request.headers["Accept-Encoding"] = "identity"
                response = await bounded_send(client, next_request, settings.response_max_bytes)
                _origin = next_origin
            response.history = history
    except (ResponseLimitError, ResponseEncodingError) as error:
        return ConsoleResult(
            ok=False,
            request=request_summary,
            error_kind=str(error),
            error_message="Response exceeded the supported transport limits",
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
    except httpx.TimeoutException:
        return ConsoleResult(
            ok=False,
            request=request_summary,
            error_kind="request_timeout",
            error_message=f"No response within {payload.timeout_ms} ms",
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
    except (httpx.HTTPError, OSError) as error:
        return ConsoleResult(
            ok=False,
            request=request_summary,
            error_kind="request_failed",
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
            body_truncated=truncated,
            body_encoding=response.encoding,
            elapsed_ms=elapsed,
            redirects=[str(item.url) for item in response.history],
        ),
        elapsed_ms=elapsed,
    )


def safe_message(error: Exception) -> str:
    """把传输层异常压缩成一行、不含本机路径的说明。

    实现：只取首行并截断到 200 字符，避免把文件路径、代理细节
    或第三方库堆栈带到界面与日志之外的地方。
    """
    text = str(error).splitlines()[0] if str(error) else type(error).__name__
    return text[:200]
