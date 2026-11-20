"""Response contracts for console HTTP requests and their history."""

from api_agent.infrastructure.api import ApiModel, UtcDatetime
from api_agent.infrastructure.outcomes import OutcomeFields


class ConsoleHistoryResponse(ApiModel, OutcomeFields):
    """一条控制台请求的历史摘要。

    实现：头已脱敏、正文只留预览，``response_bytes`` 保留真实长度，
    让界面能说清“原文多大、这里只留了多少”。
    """

    id: str
    project_id: str | None
    method: str
    url: str
    request_headers: dict
    body_mode: str
    body_preview: str | None
    ok: bool
    status_code: int | None
    elapsed_ms: float
    error_kind: str | None
    error_message: str | None
    response_headers: dict
    response_preview: str | None
    response_bytes: int
    created_at: UtcDatetime


class ConsoleHistoryDetail(ConsoleHistoryResponse):
    """Include the replay body only when a single record is opened."""

    request_body: str | None
