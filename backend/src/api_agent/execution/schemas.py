"""Bounded, data-only API testing contracts with stateful variable chains."""

import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from api_agent.execution.variables import (
    CONCRETE_PATH_SEGMENT_RE,
    PLACEHOLDER_RE,
    VAR_NAME_PATTERN,
    VAR_NAME_RE,
    find_placeholders,
)
from api_agent.infrastructure.json_pointer import is_valid as is_valid_pointer
from api_agent.infrastructure.logging import is_sensitive_key
from api_agent.infrastructure.outcomes import OutcomeFields
from api_agent.infrastructure.targets import HEADER_NAME, RESERVED_HEADERS, format_host_literal
from api_agent.projects.schemas import AuthReference

Method = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
Status = Literal["passed", "failed", "error", "timed_out", "cancelled"]
IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"
PATH_SEGMENT = CONCRETE_PATH_SEGMENT_RE
PARAM_VALUE_SEGMENT = re.compile(r"[A-Za-z0-9_~-]+")
TEMPLATE = re.compile(
    r"^/(?:[A-Za-z0-9_.~-]+|\{[A-Za-z_][A-Za-z0-9_]*\})(?:/(?:[A-Za-z0-9_.~-]+|\{[A-Za-z_][A-Za-z0-9_]*\}))*/?$"
)
FORBIDDEN_DYNAMIC_HEADERS = RESERVED_HEADERS | frozenset({"proxy-authorization"})


def _valid_request_path_segment(segment: str) -> bool:
    """线性时间校验请求路径单段（允许字面量与受控 `{{var}}`，禁止 `.`/`..`/百分号/空白）。"""
    if not segment:
        return False
    normalized = PLACEHOLDER_RE.sub("v", segment)
    return bool(PATH_SEGMENT.fullmatch(normalized))


class Input(BaseModel):
    """受控计划输入基类：严格类型、拒绝额外字段，校验错误不回显原始输入。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


def local_origin(value: str) -> str:
    """校验并规范化目标 HTTP(S) origin（支持任意合法域名、局域网 IP、回环 IP 与公网 IP）。

    仍严格禁止非 http/https 协议、内嵌账号密码、路径/查询/片段、0 端口、空白与控制字符；
    保留函数名 ``local_origin`` 以保持跨模块导入兼容性。
    """
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port == 0
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError
        host = format_host_literal(parsed.hostname)
        return f"{parsed.scheme.lower()}://{host}" + (f":{port}" if port is not None else "")
    except ValueError:
        raise ValueError(
            "origin must be a valid HTTP(S) origin without credentials or path"
        ) from None


class VariableExtract(Input):
    """从单次响应中提取运行时标量变量，供后续步骤引用。"""

    name: str = Field(pattern=VAR_NAME_PATTERN)
    source: Literal["json", "header", "status"] = "json"
    pointer: str = Field(default="", max_length=250)
    header_name: str = Field(default="", max_length=100)
    required: bool = True
    sensitive: bool = False

    @model_validator(mode="after")
    def valid_extract(self):
        """校验提取来源与指针/响应头字段组合是否自洽。"""
        if self.source == "json":
            if not is_valid_pointer(self.pointer) or self.header_name:
                raise ValueError("json extraction requires a valid JSON Pointer and no header_name")
        elif self.source == "header":
            if not HEADER_NAME.fullmatch(self.header_name) or self.pointer:
                raise ValueError("header extraction requires a valid header_name and no pointer")
        elif self.pointer or self.header_name:
            raise ValueError("status extraction takes neither pointer nor header_name")
        return self


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
        """按 ``kind`` 校验 ``pointer``/``expected`` 的组合是否合法。"""
        if self.kind == "status" and not (
            type(self.expected) is int and 100 <= self.expected <= 599
        ):
            raise ValueError("status requires an HTTP status integer")
        if self.kind in {"json_equals", "json_exists"}:
            if not is_valid_pointer(self.pointer):
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
    """一条待执行的请求用例（支持受控变量引用、前置依赖、提取规则与清理标记）。"""

    id: str = Field(pattern=IDENTIFIER)
    method: Method
    path: str = Field(max_length=300)
    query: dict[str, str] = Field(default_factory=dict, max_length=30)
    headers: dict[str, str] = Field(default_factory=dict, max_length=10)
    body: JsonValue = None
    assertions: list[Assertion] = Field(min_length=1, max_length=20)
    extract: list[VariableExtract] = Field(default_factory=list, max_length=10)
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    always_run: bool = False

    @field_validator("path")
    @classmethod
    def concrete_path(cls, value):
        """校验路径是“具体、未编码的绝对路径”或仅含受控 `{{var}}` 占位的路径。"""
        if not value.startswith("/") or (
            value != "/"
            and not all(
                _valid_request_path_segment(part) for part in value[1:].removesuffix("/").split("/")
            )
        ):
            raise ValueError("path must be a concrete, unencoded absolute path")
        return value

    @model_validator(mode="after")
    def supported_request(self):
        """限制请求可携带的头与请求体的组合。

        - `Accept` 与 `Content-Type` 仍仅允许 `application/json`；
        - 其他非保留请求头（如 `token`、`Authorization`、`X-Request-Id`）仅允许携带
          已绑定的运行时变量占位符 `{{var}}`，禁止在计划中硬编码静态凭据；
        - 无实体语义的方法（GET/HEAD/OPTIONS）不允许带 body。
        """
        lowered = [name.lower() for name in self.headers]
        if len(lowered) != len(set(lowered)):
            raise ValueError("duplicate request header names are not allowed")
        for name, value in self.headers.items():
            if not HEADER_NAME.fullmatch(name):
                raise ValueError("invalid request header name")
            low = name.lower()
            if low in FORBIDDEN_DYNAMIC_HEADERS:
                raise ValueError("reserved request header is not allowed")
            if low in {"accept", "content-type"}:
                if value != "application/json":
                    raise ValueError("only JSON headers are supported")
            else:
                if (
                    len(value) > 512
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
                    or not PLACEHOLDER_RE.search(value)
                ):
                    raise ValueError(
                        "custom headers must reference a bound runtime variable placeholder"
                    )
        if self.method in {"GET", "HEAD", "OPTIONS"} and self.body is not None:
            raise ValueError("this method does not support a JSON body")
        if len({rule.name for rule in self.extract}) != len(self.extract):
            raise ValueError("duplicate_extract_variable")
        return self


class ExecutionPlan(Input):
    """一次受控执行计划：目标、白名单、用例、变量依赖链与配额。"""

    schema_version: Literal["1"] = "1"
    origin: str
    allowed_methods: list[Method] = Field(min_length=1, max_length=7)
    path_templates: list[str] = Field(min_length=1, max_length=50)
    auth_ref: AuthReference | None = None
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)
    requests: list[RequestCase] = Field(min_length=1, max_length=50)
    max_requests: int = Field(default=50, ge=1, le=50)
    request_timeout_ms: int = Field(default=3000, ge=50, le=30_000)
    timeout_ms: int = Field(default=30_000, ge=100, le=120_000)
    delay_ms: int = Field(default=50, ge=0, le=1000)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value):
        """把 origin 归一成数字回环地址（委托 ``local_origin``）。"""
        return local_origin(value)

    @field_validator("variables")
    @classmethod
    def validate_initial_variables(cls, value: dict[str, str | int | float | bool]):
        """初始变量仅允许非敏感命名与安全标量，防止在计划里硬编码密码或密钥。"""
        for name, item in value.items():
            if not VAR_NAME_RE.fullmatch(name):
                raise ValueError("invalid_variable_name")
            if is_sensitive_key(name):
                raise ValueError("sensitive_initial_variable_not_allowed")
            if isinstance(item, str) and (
                len(item) > 1024
                or "{{" in item
                or "}}" in item
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in item)
            ):
                raise ValueError("invalid_variable_value")
        return value

    def matches_path(self, path: str, *, allow_vars: bool = False) -> bool:
        """线性时间按段比对路径是否命中任一授权模板，避免复杂正则回溯。"""
        case_parts = path.split("/")
        for template in self.path_templates:
            tmpl_parts = template.split("/")
            if len(case_parts) != len(tmpl_parts):
                continue
            matched = True
            for c_part, t_part in zip(case_parts, tmpl_parts, strict=True):
                if t_part.startswith("{") and t_part.endswith("}"):
                    candidate = PLACEHOLDER_RE.sub("v", c_part) if allow_vars else c_part
                    if not PARAM_VALUE_SEGMENT.fullmatch(candidate):
                        matched = False
                        break
                elif c_part != t_part:
                    matched = False
                    break
            if matched:
                return True
        return False

    @model_validator(mode="after")
    def valid_plan(self):
        """对整个计划做交叉校验，任何一条不满足都拒绝执行。

        核心不变量：
        - 任意未由 `variables` 或前序步骤 `extract` 声明的 `{{var}}` 依然被拒绝；
        - `depends_on` 仅允许引用前序已出现的用例 ID；
        - 只有路径模板中的 `{param}` 段才允许出现 `{{var}}` 占位符。
        """
        if len(self.requests) > self.max_requests:
            raise ValueError("request_budget_exceeded")
        if len({case.id for case in self.requests}) != len(self.requests):
            raise ValueError("duplicate_request_id")
        for template in self.path_templates:
            if template != "/" and not TEMPLATE.fullmatch(template):
                raise ValueError("invalid_path_template")
            if any(part in {".", ".."} for part in template.split("/")):
                raise ValueError("invalid_path_template")

        seen_ids: set[str] = set()
        bound_vars: set[str] = set(self.variables.keys())
        for case in self.requests:
            if case.method not in self.allowed_methods:
                raise ValueError("method_not_allowed")
            if not self.matches_path(case.path, allow_vars=True):
                raise ValueError("path_not_allowed")
            for dep in case.depends_on:
                if dep not in seen_ids:
                    raise ValueError("invalid_dependency")
            refs, invalid = find_placeholders(case.model_dump(exclude={"extract"}))
            if invalid or not refs.issubset(bound_vars):
                raise ValueError("unbound_variables_not_supported")
            bound_vars.update(rule.name for rule in case.extract)
            seen_ids.add(case.id)

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
        # 顶层非请求字段也不得包含花括号模板
        _, top_invalid = find_placeholders(self.model_dump(exclude={"requests"}))
        top_refs, _ = find_placeholders(self.model_dump(exclude={"requests"}))
        if top_invalid or top_refs:
            raise ValueError("unbound_variables_not_supported")
        return self


def origin_allowed(origin: str, allowed_origins: list[str]) -> bool:
    """判断目标 origin 是否被允许：白名单为空时放行，非空时按规范化 origin 匹配。"""
    if not allowed_origins:
        return True
    try:
        target = local_origin(origin)
    except ValueError:
        target = origin.rstrip("/").lower()
    normalized_allowed = set()
    for candidate in allowed_origins:
        try:
            normalized_allowed.add(local_origin(candidate))
        except ValueError:
            normalized_allowed.add(candidate.rstrip("/").lower())
    return target in normalized_allowed


def validate_scope(
    plan: ExecutionPlan, allowed_origins: list[str], auth_env_vars: list[str]
) -> None:
    """把计划与“本次部署允许的目标/凭据”做比对。

    当 ``allowed_origins`` 非空时强制要求 ``plan.origin`` 命中白名单；
    为空时默认不限制目标 origin。凭据变量名白名单始终强制生效。
    """
    if not origin_allowed(plan.origin, allowed_origins):
        raise ValueError("origin_not_allowed")
    if plan.auth_ref and plan.auth_ref.env_var not in auth_env_vars:
        raise ValueError("auth_reference_not_allowed")


class ExtractedVariable(BaseModel):
    """单条变量提取的脱敏观测记录。"""

    name: str
    source: Literal["json", "header", "status"] = "json"
    ok: bool
    sensitive: bool = False
    value: JsonValue = None
    message: str | None = None


class AssertionResult(BaseModel):
    """单条断言的判定及有界、脱敏后的观察值。"""

    index: int
    kind: str
    passed: bool | None = None
    message: str | None = None
    pointer: str = ""
    actual: JsonValue = None
    actual_present: bool = False
    actual_type: str | None = None
    actual_truncated: bool = False


class ResponseCapture(BaseModel):
    """一次请求实际收到的响应快照（原样输出模式才会填充）。"""

    status_text: str | None = None
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: str | None = None
    body_bytes: int = 0
    body_truncated: bool = False
    omitted: bool = False


class CaseResult(OutcomeFields):
    """单个用例的执行结果。"""

    id: str
    status: Literal["passed", "failed", "error", "timed_out", "cancelled", "skipped", "unknown"] = (
        "unknown"
    )
    http_status: int | None = None
    response_time_ms: float | None = None
    error_kind: str | None = None
    assertions: list[AssertionResult] = Field(default_factory=list)
    extracted: list[ExtractedVariable] = Field(default_factory=list)
    response: ResponseCapture | None = None


class RunResult(OutcomeFields):
    """一次执行的最终汇总。"""

    schema_version: Literal["1"] = "1"
    status: Status
    error_kind: str | None = None
    engine: str = "postman-cli"
    duration_ms: float = 0
    reserved_requests: int
    observed_attempts: int = 0
    unknown_requests: int = 0
    executions: list[CaseResult]


def unevaluated_assertions(case: RequestCase) -> list[AssertionResult]:
    """为未取得完整可评估响应的用例保留断言序号和指针，不推断通过或失败。"""
    return [
        AssertionResult(
            index=index,
            kind=assertion.kind,
            pointer=assertion.pointer,
            passed=None,
            message="Response could not be evaluated",
        )
        for index, assertion in enumerate(case.assertions)
    ]


def empty_result(plan: ExecutionPlan, status: Status, error_kind: str) -> RunResult:
    """构造一个“没有观察到任何结果”的占位返回值。"""
    return RunResult(
        status=status,
        error_kind=error_kind,
        reserved_requests=len(plan.requests),
        unknown_requests=len(plan.requests),
        executions=[
            CaseResult(id=case.id, assertions=unevaluated_assertions(case))
            for case in plan.requests
        ],
    )
