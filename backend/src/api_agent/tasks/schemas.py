"""Bounded document, endpoint and test-case contracts; documents cannot grant target scope."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, field_validator, model_validator

from api_agent.console.service import ConsoleRequest
from api_agent.infrastructure.api import ApiInput
from api_agent.infrastructure.json_pointer import is_valid


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
    source: Literal["document", "generated"] = "document"

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

    cases: list[Case] = Field(default_factory=list, max_length=100)
    generated_cases: list[Case] = Field(default_factory=list, max_length=100)


class Catalog(ApiInput):
    """All recognized operations, without silently discarding unsupported document portions."""

    format: str = "custom"
    endpoints: list[Endpoint] = Field(default_factory=list, max_length=200)
    cases: list[Case] = Field(default_factory=list, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=100)

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


class StartTask(ApiInput):
    """Run an already recognized catalog against an explicit operator-supplied origin."""

    catalog: Catalog
    origin: str = Field(max_length=2000)
    allow_writes: bool = False
    timeout_ms: int = Field(default=10000, ge=1000, le=60000)
    observation_ms: int = Field(default=1000, ge=1000, le=30000)
    max_events: int = Field(default=200, ge=1, le=2000)
    wall_time_seconds: int = Field(default=300, ge=10, le=3600)
