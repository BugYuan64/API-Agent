"""Bounded document, endpoint and test-case contracts; documents cannot grant target scope."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, field_validator, model_validator

from api_agent.console.service import ConsoleRequest
from api_agent.execution.variables import VAR_NAME_PATTERN, VAR_NAME_RE
from api_agent.infrastructure.api import ApiInput
from api_agent.infrastructure.json_pointer import is_valid
from api_agent.infrastructure.logging import is_sensitive_key
from api_agent.infrastructure.targets import HEADER_NAME


class ExtractRule(ApiInput):
    """从用例响应中提取变量供后续用例通过 `{{name}}` 占位符复用。"""

    name: str = Field(pattern=VAR_NAME_PATTERN)
    source: Literal["json", "header", "status"] = "json"
    pointer: str = Field(default="", max_length=250)
    header_name: str = Field(default="", max_length=100)
    required: bool = True
    sensitive: bool = False

    @model_validator(mode="after")
    def valid_extract(self):
        """校验提取来源与 JSON Pointer / 响应头字段是否匹配。"""
        if self.source == "json":
            if not is_valid(self.pointer) or self.header_name:
                raise ValueError("json_extract_requires_valid_pointer")
        elif self.source == "header":
            if not HEADER_NAME.fullmatch(self.header_name) or self.pointer:
                raise ValueError("header_extract_requires_header_name")
        elif self.pointer or self.header_name:
            raise ValueError("status_extract_forbids_pointer_and_header")
        return self


def _validate_initial_vars(
    value: dict[str, str | int | float | bool],
) -> dict[str, str | int | float | bool]:
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


class Check(ApiInput):
    """Supported deterministic assertions on actual HTTP, SSE or WebSocket evidence."""

    kind: Literal["status", "contains", "json_equals", "event_count", "message_contains"]
    expected: JsonValue = None
    pointer: str = ""

    @model_validator(mode="after")
    def valid_check(self):
        """Reject incompatible assertion types rather than returning misleading failures."""
        if self.kind == "status" and (
            type(self.expected) is not int or not 100 <= self.expected <= 599
        ):
            raise ValueError("status_requires_http_code")
        if self.kind == "event_count" and (type(self.expected) is not int or self.expected < 0):
            raise ValueError("event_count_requires_nonnegative_integer")
        if self.kind in {"contains", "message_contains"} and not isinstance(self.expected, str):
            raise ValueError("contains_requires_string")
        if self.kind == "json_equals" and not is_valid(self.pointer):
            raise ValueError("invalid_json_pointer")
        return self


class Case(ApiInput):
    """A transport-neutral case; relative paths stay on the operator-confirmed origin."""

    name: str = Field(default="文档用例", max_length=160)
    protocol: Literal["http", "sse", "websocket"] = "http"
    method: Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(min_length=1, max_length=1800)
    headers: dict[str, str] = Field(default_factory=dict)
    body: JsonValue = None
    messages: list[str] = Field(default_factory=list, max_length=20)
    assertions: list[Check] = Field(default_factory=list, max_length=20)
    extract: list[ExtractRule] = Field(default_factory=list, max_length=10)
    always_run: bool = False
    source: Literal["document", "generated"] = "document"
    kind: Literal["contract", "document", "test"] | None = None

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        """Reject absolute URLs, fragments and network-path references before scheduling."""
        parsed = urlsplit(value)
        if not value.startswith("/") or value.startswith("//") or parsed.netloc or parsed.fragment:
            raise ValueError("relative_path_required")
        return value

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value):
        """Apply the same header policy as interactive testing."""
        return ConsoleRequest.safe_headers(value)

    @model_validator(mode="after")
    def valid_transport(self):
        """Reject browser-forbidden HTTP bodies and ambiguous WebSocket/SSE inputs."""
        if self.protocol == "websocket" and self.body is not None:
            raise ValueError("websocket_case_does_not_use_http_body")
        if self.body is not None and self.method in {"GET", "HEAD", "OPTIONS"}:
            raise ValueError("body_not_allowed_for_method")
        if self.protocol == "sse" and self.messages:
            raise ValueError("sse_is_server_to_client_only")
        if self.protocol == "websocket" and self.method != "GET":
            raise ValueError("websocket_upgrade_requires_get")
        return self


class Endpoint(Case):
    """Recognized operation plus its examples and original test cases."""

    path_template: str | None = Field(default=None, min_length=1, max_length=1800)
    cases: list[Case] = Field(default_factory=list, max_length=100)
    generated_cases: list[Case] = Field(default_factory=list, max_length=100)


class Catalog(ApiInput):
    """All recognized operations, without silently discarding unsupported document portions."""

    format: str = "custom"
    title: str | None = Field(default=None, max_length=160)
    server_url: str | None = Field(default=None, max_length=2000)
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)
    endpoints: list[Endpoint] = Field(default_factory=list, max_length=200)
    cases: list[Case] = Field(default_factory=list, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("variables")
    @classmethod
    def safe_variables(cls, value):
        return _validate_initial_vars(value)

    @field_validator("title", mode="before")
    @classmethod
    def compact_title(cls, value):
        """Keep document and generated labels to one bounded line."""
        if not isinstance(value, str):
            return value
        return " ".join(value.split())[:160] or None

    @field_validator("server_url", mode="before")
    @classmethod
    def safe_server_hint(cls, value):
        """Accept absolute HTTP(S) base URLs as form hints, without contacting them."""
        if not isinstance(value, str):
            return None
        value = value.strip()
        if len(value) > 2000 or any(
            char.isspace() or ord(char) < 32 or char in "?#\\{}" for char in value
        ):
            return None
        try:
            url = urlsplit(value)
            if (
                url.scheme in {"http", "https"}
                and url.hostname
                and url.username is None
                and url.password is None
                and url.port != 0
            ):
                return value
        except ValueError:
            pass
        return None

    @model_validator(mode="after")
    def bounded_catalog(self):
        """Bound nesting, case volume and serialized input before persisting a task."""
        if len(self.model_dump_json().encode()) > 1_048_576:
            raise ValueError("catalog_too_large")
        case_count = len(self.cases) + sum(
            1 + len(endpoint.cases) + len(endpoint.generated_cases) for endpoint in self.endpoints
        )
        if case_count > 200:
            raise ValueError("case_limit_exceeded")
        if not self.endpoints and not self.cases:
            raise ValueError("document_contains_no_cases")
        return self


class DocumentInput(ApiInput):
    """UTF-8 text uploaded by the browser; binary document formats are explicitly unsupported."""

    filename: str = Field(default="document.json", max_length=200)
    text: str = Field(min_length=1, max_length=524288)

    @field_validator("text")
    @classmethod
    def byte_limit(cls, value):
        """Bound encoded bytes as well as character count."""
        if len(value.encode()) > 524288 or "\x00" in value:
            raise ValueError("document_too_large_or_binary")
        return value


class RecognizeDocument(DocumentInput):
    """Optionally prepare editable model-generated cases during recognition."""

    tests_per_url: int | None = Field(default=None, ge=1, le=10)
    previous: Catalog | None = None


class RegenerateTests(ApiInput):
    """Regenerate only the selected operations of the reviewed draft."""

    catalog: Catalog
    document: DocumentInput | None = None
    tests_per_url: int = Field(ge=1, le=10)
    selected: list[int] = Field(min_length=1, max_length=200)


class TaskRunMetadata(ApiInput):
    """Metadata describing the origin, display badge and generation quota of a task run."""

    source: Literal["manual", "ai"] = "manual"
    badge: str | None = Field(default=None, max_length=40)
    tests_per_url: int | None = Field(default=None, ge=1, le=10)


class TaskRunOptions(ApiInput):
    """Current target and execution settings used for both new and repeated runs."""

    origin: str = Field(max_length=2000)
    allow_writes: bool = False
    timeout_ms: int = Field(default=30000, ge=1000, le=60000)
    observation_ms: int = Field(default=1000, ge=1000, le=30000)
    max_events: int = Field(default=200, ge=1, le=2000)
    wall_time_seconds: int = Field(default=300, ge=10, le=3600)
    rounds: int = Field(default=1, ge=1, le=20)
    cooldown_ms: int = Field(default=500, ge=0, le=60_000)
    reset_vars_per_round: bool = True
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)

    @field_validator("variables")
    @classmethod
    def safe_run_variables(cls, value):
        return _validate_initial_vars(value)


class StartTask(TaskRunOptions):
    """Compile a recognized document catalog into cases for the first run."""

    catalog: Catalog
    document: DocumentInput | None = None
    source: Literal["manual", "ai"] = "manual"
    badge: str | None = Field(default=None, max_length=40)


class RerunTask(TaskRunOptions):
    """Optionally replace reviewed inputs at their original snapshot indices."""

    case_edits: dict[int, Case] = Field(default_factory=dict, max_length=200)
    runtime_headers: dict[str, str] = Field(default_factory=dict)
    source: Literal["manual", "ai"] = "manual"
    badge: str | None = Field(default=None, max_length=40)

    @field_validator("runtime_headers")
    @classmethod
    def safe_runtime_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """复用交互式请求头策略校验临时重测请求头，绝不落盘持久化明文凭据。"""
        return ConsoleRequest.safe_headers(value)
