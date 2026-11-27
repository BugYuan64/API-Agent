"""Target URL and header policy shared by the local console, tasks, and stream probes."""

import ipaddress
import re
from urllib.parse import SplitResult, urlsplit

from api_agent.infrastructure.logging import is_sensitive_key

HTTP_SCHEMES = frozenset({"http", "https"})
STREAM_SCHEMES = HTTP_SCHEMES | frozenset({"ws", "wss"})
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
HOSTNAME_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
# 由传输层自行管理的头：允许覆盖会破坏握手或改变报文语义。
RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "proxy-connection",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
    }
)


class TargetError(ValueError):
    """策略拒绝访问的目标 URL。

    作用：把“协议不支持 / URL 非法 / 含内嵌凭据 / 不在允许列表 / 头不合法”这些拒绝原因
    收敛成一个异常类型，路由层据此返回固定的错误码，而不是抛 500。
    实现：直接继承 ``ValueError``，消息本身就是稳定错误码（如 ``target_origin_not_allowed``）。
    """


def format_host_literal(host: str) -> str:
    """校验并规范化主机名（支持 IPv4、IPv6 与合法域名）。

    非法主机名（空串、含非法字符、空标签或超长标签）抛出 ``ValueError``。
    """
    if not host or len(host) > 253:
        raise ValueError("invalid host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        stripped = host[:-1] if host.endswith(".") else host
        if not stripped or ".." in stripped:
            raise ValueError("invalid host") from None
        try:
            ascii_host = stripped.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("invalid host") from None
        if len(ascii_host) > 253:
            raise ValueError("invalid host") from None
        labels = ascii_host.split(".")
        if not all(HOSTNAME_LABEL.fullmatch(label) for label in labels):
            raise ValueError("invalid host") from None
        return ascii_host
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def parse_target(value: str, schemes: frozenset[str] = HTTP_SCHEMES) -> SplitResult:
    """解析并校验一个目标 URL，返回 ``urlsplit`` 结果。

    作用：控制台、智能群测与长连接共用此策略入口：
    允许任意合法的 ``http(s)`` / ``ws(s)`` 域名、局域网 IP、回环 IP 与公网 IP，
    同时禁止非法协议、URL 内嵌账号密码、片段、0 端口、空白与控制字符。
    """
    if not isinstance(value, str) or len(value) > 2000:
        raise TargetError("target_url_too_long")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise TargetError("target_url_invalid") from None
    if parsed.scheme.lower() not in schemes:
        raise TargetError("target_scheme_not_allowed")
    host = parsed.hostname
    if not host:
        raise TargetError("target_url_invalid")
    try:
        format_host_literal(host)
    except ValueError:
        raise TargetError("target_url_invalid") from None
    if parsed.username is not None or parsed.password is not None:
        raise TargetError("target_credentials_not_allowed")
    if parsed.fragment or port == 0:
        raise TargetError("target_url_invalid")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise TargetError("target_url_invalid")
    if parsed.path and not parsed.path.startswith("/"):
        raise TargetError("target_url_invalid")
    return parsed


def target_origin(parsed: SplitResult) -> str:
    """按规范重建 origin 字符串（IPv6 加方括号、域名小写、仅在有端口时拼端口）。

    实现：``ws``/``wss`` 是长连接专用协议，但允许列表里存的是 http(s)，
    因此这里保留原协议小写重建，由 ``policy_origin`` 负责映射后再比对。
    """
    literal = format_host_literal(parsed.hostname or "")
    return f"{parsed.scheme.lower()}://{literal}" + (
        f":{parsed.port}" if parsed.port is not None else ""
    )


def policy_origin(origin: str) -> str:
    """把 origin 映射成用于比对允许列表的形式（ws→http、wss→https）。

    实现：只替换前缀，其余部分（主机与端口）保持原样，
    这样 ``ws://api.example.com:8080`` 与 ``http://api.example.com:8080`` 被视为同一源。
    """
    if origin.startswith("ws://"):
        return "http://" + origin[len("ws://") :]
    if origin.startswith("wss://"):
        return "https://" + origin[len("wss://") :]
    return origin


def _normalize_allowed_origin(origin_value: str) -> str:
    try:
        return policy_origin(target_origin(parse_target(origin_value, STREAM_SCHEMES)))
    except TargetError:
        return origin_value.rstrip("/").lower()


def validate_target(
    value: str, allowed_origins: list[str], schemes: frozenset[str] = HTTP_SCHEMES
) -> tuple[SplitResult, str]:
    """校验目标 URL 并返回 ``(解析结果, 规范 origin)``。

    作用：这是所有“由本进程发出请求”的入口共用的策略函数，
    保证控制台、智能群测、SSE、WebSocket 的范围判定完全一致。
    实现：先做 URL 合法性与协议/凭据护栏（``parse_target``）；若部署显式配置了
    ``TARGET_ALLOWED_ORIGINS`` 白名单，则要求映射后的 origin 出现在列表里；
    白名单为空时默认不限制目标主机。
    """
    parsed = parse_target(value, schemes)
    origin = target_origin(parsed)
    if allowed_origins and policy_origin(origin) not in {
        _normalize_allowed_origin(origin_value) for origin_value in allowed_origins
    }:
        raise TargetError("target_origin_not_allowed")
    return parsed, origin


def validate_headers(headers: dict[str, str]) -> dict[str, str]:
    """校验并返回小写化的请求头。

    作用：用户可自定义鉴权等头，但传输层关键头不能让用户覆盖，
    否则可能改变报文语义（如伪造 Host、破坏 WebSocket 握手）。
    实现：头名必须命名字符集合法且不重复（大小写不敏感），
    不在保留集合里；值不得含控制字符，长度不超过 4096。
    返回小写键的新字典，避免 ``Authorization`` 与 ``authorization`` 同时存在。
    """
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
            raise TargetError("target_header_invalid")
        lowered = name.lower()
        if lowered in RESERVED_HEADERS:
            raise TargetError("target_header_reserved")
        if lowered in normalized:
            raise TargetError("target_header_duplicate")
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise TargetError("target_header_invalid")
        normalized[lowered] = value
    if len(normalized) > 20:
        raise TargetError("target_header_limit")
    return normalized


def redact_headers(headers: dict[str, str] | dict[str, list[str]]) -> dict:
    """把敏感请求/响应头替换成占位符，用于落库与历史展示。

    作用：控制台的即时响应仍然保留真实值（本机操作者当场核对），
    但持久化到 SQLite 的历史记录不应保存凭据与 Cookie。
    实现：键名判定统一交给 ``is_sensitive_key``（覆盖 ``token``、
    ``X-Auth-Token``、``X-Access-Token`` 等写法），命中就整体替换；
    键名保持原样写回，这样历史里看到的仍是用户/目标原本书写的头名。
    值列表逐项替换并保持列表形态（响应侧多值头不会退化成字符串）。
    """
    return {
        name: (["[redacted]"] * len(value) if isinstance(value, list) else "[redacted]")
        if is_sensitive_key(name)
        else value
        for name, value in headers.items()
    }
