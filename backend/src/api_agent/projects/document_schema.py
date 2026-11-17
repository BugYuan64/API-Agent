"""定义源码生成接口文档的模型输出契约，保留证据与未知项并限制结构大小。"""

import json
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from api_agent.infrastructure.api import ApiInput

DocumentMethod = Literal[
    "GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE", "TRACE", "WEBSOCKET"
]


def validate_schema(value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
    """限制模型提供的 JSON Schema 大小、嵌套与引用范围，避免导出隐式外部依赖。"""
    if value is None:
        return None
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 262_144:
        raise ValueError("document_schema_too_large")
    pending = [(value, 0)]
    nodes = 0
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if depth > 24 or nodes > 10_000:
            raise ValueError("document_schema_too_complex")
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                reference = node.get(key)
                if reference is not None and (
                    not isinstance(reference, str) or not reference.startswith("#")
                ):
                    raise ValueError("document_external_reference")
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return value


class DocumentParameter(ApiInput):
    """一个输入参数的来源与约束，未知类型使用空字符串，复杂结构可保留 Schema。"""

    name: str = Field(min_length=1, max_length=300)
    location: Literal["query", "path", "header", "cookie"]
    required: bool = False
    type: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=6000)
    schema_definition: dict[str, JsonValue] | None = None

    _schema = field_validator("schema_definition")(validate_schema)


class DocumentResponse(ApiInput):
    """一个有源码依据的响应状态与正文，default 表示源码未明确具体状态。"""

    status: str = Field(default="default", max_length=7)
    description: str = Field(default="", max_length=12000)
    body_schema: dict[str, JsonValue] | None = None
    content_type: str = Field(default="", max_length=200)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        """仅接受 OpenAPI 响应键，避免把任意模型文字用作响应状态。"""
        if value != "default" and not re.fullmatch(r"[1-5](?:[0-9]{2}|XX)", value):
            raise ValueError("invalid_document_response_status")
        return value

    _schema = field_validator("body_schema")(validate_schema)


class DocumentEndpoint(ApiInput):
    """一个接口及其源码证据，允许保存未知字段说明，不以模型猜测补造契约。"""

    method: DocumentMethod
    path: str = Field(min_length=1, max_length=2000)
    summary: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=16000)
    parameters: list[DocumentParameter] = Field(default_factory=list, max_length=300)
    request_body: str = Field(default="", max_length=16000)
    request_schema: dict[str, JsonValue] | None = None
    request_content_type: str = Field(default="", max_length=200)
    responses: list[DocumentResponse] = Field(default_factory=list, max_length=100)
    authentication: str = Field(default="", max_length=6000)
    source_files: list[str] = Field(min_length=1, max_length=300)
    uncertainties: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("method", mode="before")
    @classmethod
    def normalize_method(cls, value):
        """统一大小写并接受 WS 简写，保留 HTTP 与 WebSocket 的协议差异。"""
        if isinstance(value, str):
            return "WEBSOCKET" if value.upper() in {"WS", "WEBSOCKET"} else value.upper()
        return value

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        """仅保留相对服务根目录的路径，禁止外部 URL、查询与控制字符。"""
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "?" in value
            or "#" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("invalid_document_path")
        return value

    @field_validator("source_files")
    @classmethod
    def relative_sources(cls, values: list[str]) -> list[str]:
        """要求引用项目内相对文件名，真实文件归属由批次编排器再次核对。"""
        result = []
        for value in values:
            normalized = value.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                not normalized
                or path.is_absolute()
                or ".." in path.parts
                or ":" in normalized
                or any(ord(char) < 32 for char in normalized)
                or len(normalized) > 1000
            ):
                raise ValueError("invalid_document_source")
            if normalized not in result:
                result.append(normalized)
        return result

    _schema = field_validator("request_schema")(validate_schema)


class DocumentBatch(ApiInput):
    """一次模型分析的完整输出，reviewed_files 用于校验所有分块均被处理。"""

    endpoints: list[DocumentEndpoint] = Field(default_factory=list, max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=300)
    reviewed_files: list[str] = Field(max_length=5000)
