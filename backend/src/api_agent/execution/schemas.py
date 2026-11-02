"""Bounded, data-only API testing contracts."""

import ipaddress
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from api_agent.projects.schemas import AuthReference

Method = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
Status = Literal["passed", "failed", "error", "timed_out", "cancelled"]
IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"
PATH_SEGMENT = re.compile(r"[A-Za-z0-9_~-]+(?:\.[A-Za-z0-9_~-]+)*")
TEMPLATE = re.compile(
    r"^/(?:[A-Za-z0-9_.~-]+|\{[A-Za-z_][A-Za-z0-9_]*\})(?:/(?:[A-Za-z0-9_.~-]+|\{[A-Za-z_][A-Za-z0-9_]*\}))*/?$"
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


def local_origin(value: str) -> str:
    """强制把 origin 归一成“数字形式的回环地址”。

    作用：本阶段只允许访问本机目标（127.0.0.1 / ::1 等），
    并且必须是 IP 字面量——这样就无法用 DNS 域名把请求引流到外部主机。
    实现：``urlsplit`` 后把 hostname 交给 ``ipaddress.ip_address`` 解析
    （域名会直接抛错），再断言 ``is_loopback``、无账号密码、无路径/查询/片段、
    端口非 0、无空白与控制字符；最后按规范重建字符串——
    IPv6 用 ``[compressed]`` 加方括号，端口存在才拼接。
    """
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not address.is_loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port == 0
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")
    except ValueError:
        raise ValueError("origin must be a numeric loopback HTTP(S) origin") from None


class Assertion(Input):
    """单条断言：期望在响应中观察到什么。

    作用：断言种类固定为 5 种（状态码、JSON 相等、JSON 存在、内容类型、响应时间），
    足以表达契约检查，又不会引入任意脚本执行能力。
    实现：``kind`` 用 Literal 约束；跨字段规则放在 ``valid_assertion`` 里，
    包括 pointer 只对 JSON 断言有效、json_exists 不接受期望值、
    content_type 只支持 application/json、response_time 必须是正的毫秒数。
    """

    kind: Literal["status", "json_equals", "json_exists", "content_type", "response_time"]
    pointer: str = Field(default="", max_length=250)
    expected: JsonValue = None

    @model_validator(mode="after")
    def valid_assertion(self):
        """按 ``kind`` 校验 ``pointer``/``expected`` 的组合是否合法。

        实现：逐类检查——
        status 要求 expected 是 100..599 的整数（用 ``type is int`` 排除 bool）；
        JSON 断言要求 pointer 是合法 JSON Pointer；
        json_equals 会递归遍历期望值，拒绝超过 JS 安全整数范围（2^53-1）的数字，
        因为 Postman 的断言脚本用 JS 比较，超范围会静默失真；
        其余类型不允许带 pointer，且各自有专属的 expected 约束。
        """
        if self.kind == "status" and not (
            type(self.expected) is int and 100 <= self.expected <= 599
        ):
            raise ValueError("status requires an HTTP status integer")
        if self.kind in {"json_equals", "json_exists"}:
            if not re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", self.pointer):
                raise ValueError("invalid JSON Pointer")
        if self.kind == "json_equals":
            pending = [self.expected]
            while pending:
                value = pending.pop()
                if type(value) in {int, float} and abs(value) > 9_007_199_254_740_991:
                    raise ValueError("JSON equality numbers exceed Postman's exact integer range")
                if isinstance(value, dict):
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
        if self.kind not in {"json_equals", "json_exists"} and self.pointer:
            raise ValueError("pointer only applies to JSON assertions")
        if self.kind == "json_exists" and self.expected is not None:
            raise ValueError("json_exists takes no expected value")
        if self.kind == "content_type" and self.expected != "application/json":
            raise ValueError("only application/json response contracts are supported")
        if self.kind == "response_time" and not (
            type(self.expected) in {int, float} and 0 < self.expected <= 60_000
        ):
            raise ValueError("response_time requires a positive millisecond limit")
        return self


class RequestCase(Input):
    """一条待执行的请求用例（路径必须是具体路径，不是模板）。

    实现：``id`` 用字符集加长度约束，作为结果配对与去重的键；
    ``path`` 由 ``concrete_path`` 逐段校验——不允许变量占位、百分号编码和 ``..``，
    确保真正发出的路径不会越出已授权模板；``headers`` 只允许 Accept/Content-Type；
    ``supported_request`` 额外禁止在 GET/HEAD/OPTIONS 上携带请求体。
    """

    id: str = Field(pattern=IDENTIFIER)
    method: Method
    path: str = Field(max_length=300)
    query: dict[str, str] = Field(default_factory=dict, max_length=30)
    headers: dict[str, str] = Field(default_factory=dict, max_length=2)
    body: JsonValue = None
    assertions: list[Assertion] = Field(min_length=1, max_length=20)

    @field_validator("path")
    @classmethod
    def concrete_path(cls, value):
        """校验路径是“具体、未编码的绝对路径”。

        实现：必须以 ``/`` 开头（根路径 ``/`` 特例放行）；
        其余情况先去掉尾部斜杠再按 ``/`` 切段，每段都要匹配 ``PATH_SEGMENT``
        （字母数字下划线波浪线中划线加点）。这样 ``%2e%2e``、``*``、``{id}``、
        ``..`` 一律被拒绝，避免编码绕过和路径穿越。
        """
        if not value.startswith("/") or (
            value != "/"
            and not all(
                PATH_SEGMENT.fullmatch(part) for part in value[1:].removesuffix("/").split("/")
            )
        ):
            raise ValueError("path must be a concrete, unencoded absolute path")
        return value

    @model_validator(mode="after")
    def supported_request(self):
        """限制请求可携带的头与请求体的组合。

        实现：头名转小写后必须唯一且只允许 accept/content-type（防止覆盖
        Host、Authorization 等关键头）；头值只允许 application/json；
        无实体语义的方法（GET/HEAD/OPTIONS）不允许带 body。
        """
        lowered = [name.lower() for name in self.headers]
        if len(lowered) != len(set(lowered)) or set(lowered) - {"accept", "content-type"}:
            raise ValueError("only unique Accept and Content-Type headers are supported")
        if any(value != "application/json" for value in self.headers.values()):
            raise ValueError("only JSON headers are supported")
        if self.method in {"GET", "HEAD", "OPTIONS"} and self.body is not None:
            raise ValueError("this method does not support a JSON body")
        return self


class ExecutionPlan(Input):
    """一次受控执行计划：目标、白名单、用例与配额。

    作用：这是唯一被接受的外部输入契约，所有限制都在这一层落地，
    CLI/MCP 只负责搬运，不再二次解释。
    实现：字段级先校验 origin（必须回环 IP）；
    模型级 ``valid_plan`` 再统一检查请求数、ID 唯一性、路径模板与用例的匹配、
    鉴权头黑名单、体积上限和未绑定变量，理由见各检查点。
    """

    schema_version: Literal["1"] = "1"
    origin: str
    allowed_methods: list[Method] = Field(min_length=1, max_length=7)
    path_templates: list[str] = Field(min_length=1, max_length=50)
    auth_ref: AuthReference | None = None
    requests: list[RequestCase] = Field(min_length=1, max_length=50)
    max_requests: int = Field(default=50, ge=1, le=50)
    request_timeout_ms: int = Field(default=3000, ge=50, le=10_000)
    timeout_ms: int = Field(default=30_000, ge=100, le=120_000)
    delay_ms: int = Field(default=50, ge=0, le=1000)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value):
        """把 origin 归一成数字回环地址（委托 ``local_origin``）。

        实现：这里只做转发，实际校验逻辑集中在 ``local_origin``，
        以便其它地方（如测试）可以直接复用同一套规则。
        """
        return local_origin(value)

    @model_validator(mode="after")
    def valid_plan(self):
        """对整个计划做交叉校验，任何一条不满足都拒绝执行。

        实现顺序与理由：
        ① 请求数不得超过 ``max_requests``；
        ② 用例 ID 不得重复（结果靠 ID 配对）；
        ③ 每个路径模板转成正则：``{变量}`` 段替换成“单段字面量”模式，
           字面段用 ``re.escape`` 转义，并拒绝 ``.``/``..`` 段；
        ④ 逐用例检查方法在白名单内、路径能匹配某个模板（这是访问面收口的关键）；
        ⑤ 鉴权头不许撞上 Host/Content-Length/Transfer-Encoding/Connection/
           Accept/Content-Type/Proxy-* 等会改变协议语义的头；
        ⑥ 序列化后检查体积（≤256KiB）且 JSON 中无 NaN/Infinity；
        ⑦ 遍历全部字符串，拒绝 ``{{ }}`` 未绑定变量——Postman 会把它当变量替换，
           可能绕过我们已校验的固定字面量。
        """
        if len(self.requests) > self.max_requests:
            raise ValueError("request_budget_exceeded")
        if len({case.id for case in self.requests}) != len(self.requests):
            raise ValueError("duplicate_request_id")
        patterns = []
        for template in self.path_templates:
            if template != "/" and not TEMPLATE.fullmatch(template):
                raise ValueError("invalid_path_template")
            if any(part in {".", ".."} for part in template.split("/")):
                raise ValueError("invalid_path_template")
            pattern = "/".join(
                "[A-Za-z0-9_~-]+" if part.startswith("{") else re.escape(part)
                for part in template.split("/")
            )
            patterns.append(re.compile(pattern))
        for case in self.requests:
            if case.method not in self.allowed_methods:
                raise ValueError("method_not_allowed")
            if not any(pattern.fullmatch(case.path) for pattern in patterns):
                raise ValueError("path_not_allowed")
        if (
            self.auth_ref
            and self.auth_ref.header_name
            and self.auth_ref.header_name.lower()
            in {
                "host",
                "content-length",
                "transfer-encoding",
                "connection",
                "accept",
                "content-type",
                "proxy-authorization",
                "proxy-connection",
            }
        ):
            raise ValueError("auth_header_not_allowed")
        try:
            encoded = json.dumps(self.model_dump(), allow_nan=False, ensure_ascii=True)
        except ValueError:
            raise ValueError("non_finite_json_value") from None
        if len(encoded.encode()) > 262_144:
            raise ValueError("plan_too_large")
        pending = [self.model_dump()]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and ("{{" in value or "}}" in value):
                raise ValueError("unbound_variables_not_supported")
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return self


def validate_scope(
    plan: ExecutionPlan, allowed_origins: list[str], auth_env_vars: list[str]
) -> None:
    """把计划与“本次部署允许的目标/凭据”做比对。

    作用：计划自带 origin，但能不能打要由本地配置说了算——
    这是防止越权访问的第二道闸（第一道是 ``local_origin`` 的回环限制）。
    实现：origin 比对时对配置项再 ``rstrip("/")`` 一次，避免形态差异造成误判；
    若计划引用了凭据，其变量名必须出现在 ``TARGET_AUTH_ENV_VARS`` 白名单里。
    失败时抛出带固定错误码的 ValueError，由调用方转成 error_kind。
    """
    if plan.origin not in {origin.rstrip("/") for origin in allowed_origins}:
        raise ValueError("origin_not_allowed")
    if plan.auth_ref and plan.auth_ref.env_var not in auth_env_vars:
        raise ValueError("auth_reference_not_allowed")


class AssertionResult(BaseModel):
    """单条断言的观察结果（只暴露元信息，不含响应内容）。

    作用：让上层知道“第几条、什么类型、是否通过”，但拿不到响应体的任何片段，
    从结构上避免把目标数据或错误文案带出。
    实现：``passed`` 允许为 None（无法判定时），``index`` 与计划中的断言顺序一一对应；
    ``message`` 仅在断言失败时携带引擎给出的差异说明，正常通过时为 None。
    """

    index: int
    kind: str
    passed: bool | None = None
    message: str | None = None


class ResponseCapture(BaseModel):
    """一次请求实际收到的响应快照（原样输出模式才会填充）。

    作用：让调用方能看到目标真实返回了什么，而不是只有“通过/失败”一个布尔值。
    实现：``headers`` 保留引擎给出的键值对顺序（多值头合并为列表），
    ``body`` 是解码后的文本；解码失败时 ``body`` 为 None 并置 ``body_truncated``，
    以免二进制响应把整个结果撑爆。``omitted`` 为 True 表示本次运行关闭了捕获。
    """

    status_text: str | None = None
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: str | None = None
    body_bytes: int = 0
    body_truncated: bool = False
    omitted: bool = False


class CaseResult(BaseModel):
    """单个用例的执行结果。

    实现：默认 ``status="unknown"``——只有确实观察到证据才会被改成
    passed/failed/error，避免把“没跑到”误报成“通过”；
    ``error_kind`` 用固定码描述失败类别（如 request_error、missing_assertions）；
    ``response`` 在开启响应捕获时携带目标回包，关闭时为 None。
    """

    id: str
    status: Literal["passed", "failed", "error", "unknown"] = "unknown"
    http_status: int | None = None
    response_time_ms: float | None = None
    error_kind: str | None = None
    assertions: list[AssertionResult] = Field(default_factory=list)
    response: ResponseCapture | None = None


class RunResult(BaseModel):
    """一次执行的最终汇总。

    实现：``reserved_requests`` 是预算口径（计划里声明了多少），
    ``observed_attempts`` 是实际观察到报告里有多少条，``unknown_requests`` 是差额，
    三者的关系让上层能判断“预算是全部用掉了，还是部分根本没发出”。
    """

    schema_version: Literal["1"] = "1"
    status: Status
    error_kind: str | None = None
    engine: str = "postman-cli"
    duration_ms: float = 0
    reserved_requests: int
    observed_attempts: int = 0
    unknown_requests: int = 0
    executions: list[CaseResult]


def empty_result(plan: ExecutionPlan, status: Status, error_kind: str) -> RunResult:
    """构造一个“没有观察到任何结果”的占位返回值。

    作用：在 CLI 缺失、报告缺失、超时、解析失败等情形下，
    仍需返回与计划结构对齐的对象，让上层（和用户）能区分
    “跑了但全失败”和“根本没跑起来”。
    实现：``reserved_requests`` 与 ``unknown_requests`` 都设为计划里的请求数
    （即全部预算被占用但结果未知），``executions`` 为每个用例生成一条
    ``status="unknown"`` 的空结果。
    """
    return RunResult(
        status=status,
        error_kind=error_kind,
        reserved_requests=len(plan.requests),
        unknown_requests=len(plan.requests),
        executions=[CaseResult(id=case.id) for case in plan.requests],
    )
