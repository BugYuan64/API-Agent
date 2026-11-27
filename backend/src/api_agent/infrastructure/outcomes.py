"""集中派生执行结果分类，区分本项目状态与目标真实 HTTP 状态。

依赖 Pydantic 提供只读计算字段；不读取数据库或异常原文，不改写原有 error_kind，
因此旧记录可按同一规则展示。传输错误说明只使用固定文本，避免泄漏连接细节。
"""

import ssl
from dataclasses import dataclass

from pydantic import BaseModel, computed_field


@dataclass(frozen=True)
class Outcome:
    """一项稳定业务结果，status 是本项目语义码，不是目标 HTTP 响应。"""

    code: str
    status: int | None
    message: str


OUTCOMES = {
    "RUNNING": Outcome("RUNNING", None, "执行仍在进行"),
    "PASSED": Outcome("PASSED", 200, "执行通过"),
    "COMPLETED": Outcome("COMPLETED", 200, "会话已正常结束"),
    "LIMIT_REACHED": Outcome("LIMIT_REACHED", None, "已达到会话观察上限"),
    "HANDSHAKE_REJECTED": Outcome("HANDSHAKE_REJECTED", 502, "目标拒绝长连接握手"),
    "INTERRUPTED": Outcome("INTERRUPTED", None, "执行因进程中断而结束，结果不完整"),
    "HTTP_RESPONSE": Outcome("HTTP_RESPONSE", 200, "已收到目标 HTTP 响应"),
    "ASSERTION_FAILED": Outcome("ASSERTION_FAILED", 422, "目标响应未满足测试断言"),
    "REQUEST_TIMEOUT": Outcome("REQUEST_TIMEOUT", 504, "请求未在时限内完整返回"),
    "EXECUTION_TIMEOUT": Outcome("EXECUTION_TIMEOUT", 504, "执行总时限已到"),
    "CANCELLED": Outcome("CANCELLED", None, "执行已取消"),
    "UNKNOWN_RESULT": Outcome("UNKNOWN_RESULT", None, "缺少足够证据判断执行结果"),
    "CONNECTION_FAILED": Outcome("CONNECTION_FAILED", 502, "未能完成目标连接或传输"),
    "TLS_ERROR": Outcome("TLS_ERROR", 502, "目标 TLS 连接失败"),
    "RESPONSE_LIMIT_EXCEEDED": Outcome("RESPONSE_LIMIT_EXCEEDED", 413, "目标响应超过读取上限"),
    "RESPONSE_ENCODING_ERROR": Outcome("RESPONSE_ENCODING_ERROR", 502, "目标响应编码无法处理"),
    "INVALID_REPORT": Outcome("INVALID_REPORT", 502, "执行报告缺失、无效或不完整"),
    "ENGINE_UNAVAILABLE": Outcome("ENGINE_UNAVAILABLE", 503, "所选执行引擎不可用"),
    "POLICY_REJECTED": Outcome("POLICY_REJECTED", 400, "请求被本地执行策略拒绝"),
    "BUDGET_EXHAUSTED": Outcome("BUDGET_EXHAUSTED", 429, "执行预算已耗尽"),
    "EXECUTION_ERROR": Outcome("EXECUTION_ERROR", 500, "执行未能完成"),
    "MODEL_TIMEOUT": Outcome("MODEL_TIMEOUT", 504, "模型服务未在时限内返回"),
    "MODEL_ERROR": Outcome("MODEL_ERROR", 502, "模型服务调用或输出校验失败"),
    "MODEL_AUTH_FAILED": Outcome(
        "MODEL_AUTH_FAILED", 502, "模型供应商鉴权失败，请检查模型服务配置"
    ),
}

ERROR_CODES = {
    "request_timeout": "REQUEST_TIMEOUT",
    "connect_timeout": "REQUEST_TIMEOUT",
    "model_timeout": "MODEL_TIMEOUT",
    "execution_timeout": "EXECUTION_TIMEOUT",
    "deadline_exceeded": "EXECUTION_TIMEOUT",
    "wall_time_exhausted": "EXECUTION_TIMEOUT",
    "tls_error": "TLS_ERROR",
    "request_failed": "CONNECTION_FAILED",
    "request_error": "CONNECTION_FAILED",
    "model_connection_failed": "MODEL_ERROR",
    "model_response_error": "MODEL_ERROR",
    "invalid_model_output": "MODEL_ERROR",
    "model_not_configured": "MODEL_ERROR",
    "model_authentication_failed": "MODEL_AUTH_FAILED",
    "connection_failed": "CONNECTION_FAILED",
    "connect_failed": "CONNECTION_FAILED",
    "response_limit_exceeded": "RESPONSE_LIMIT_EXCEEDED",
    "response_decode_error": "RESPONSE_ENCODING_ERROR",
    "response_encoding_not_supported": "RESPONSE_ENCODING_ERROR",
    "unsupported_content_encoding": "RESPONSE_ENCODING_ERROR",
    "invalid_report": "INVALID_REPORT",
    "report_unavailable": "INVALID_REPORT",
    "invalid_response_metadata": "INVALID_REPORT",
    "missing_assertions": "INVALID_REPORT",
    "invalid_assertions": "INVALID_REPORT",
    "incomplete_or_error_report": "INVALID_REPORT",
    "cli_unavailable": "ENGINE_UNAVAILABLE",
    "unknown_engine": "ENGINE_UNAVAILABLE",
    "request_budget_exhausted": "BUDGET_EXHAUSTED",
    "request_budget_exceeded": "BUDGET_EXHAUSTED",
    "target_budget_exhausted": "BUDGET_EXHAUSTED",
    "model_budget_exhausted": "BUDGET_EXHAUSTED",
    "auth_reference_unavailable": "POLICY_REJECTED",
    "auth_reference_not_allowed": "POLICY_REJECTED",
    "unsafe_variable_value": "POLICY_REJECTED",
    "missing_variable": "EXECUTION_ERROR",
    "variable_extraction_failed": "ASSERTION_FAILED",
    "dependency_failed": "EXECUTION_ERROR",
    "invalid_plan": "POLICY_REJECTED",
    "invalid_url": "POLICY_REJECTED",
    "handshake_rejected": "HANDSHAKE_REJECTED",
    "limits_exceeded": "LIMIT_REACHED",
    "recovered_after_restart": "INTERRUPTED",
    "interrupted": "INTERRUPTED",
    "dispatch_interrupted": "UNKNOWN_RESULT",
    "client_stopped": "CANCELLED",
    "cancelled": "CANCELLED",
    "execution_cancelled": "CANCELLED",
    "service_shutdown": "CANCELLED",
}


def outcome_for(
    status: str | None = None,
    error_kind: str | None = None,
    *,
    stop_reason: str | None = None,
    ok: bool | None = None,
) -> Outcome:
    """由既有生命周期和稳定错误码派生结果，未知错误保守归为执行错误。

    运行中、取消及未知终态优先；控制台 ok=True 只代表收到响应，不判断目标
    HTTP 是否成功。错误/停止原因按显式表匹配，不分析异常自然语言。
    """
    if status in {"queued", "running", "connecting", "open"}:
        return OUTCOMES["RUNNING"]
    if status == "cancelled":
        return OUTCOMES["CANCELLED"]
    if status == "interrupted":
        return OUTCOMES["INTERRUPTED"]
    if status == "unknown":
        return OUTCOMES["UNKNOWN_RESULT"]
    if ok is True:
        return OUTCOMES["HTTP_RESPONSE"]
    for reason in (error_kind, stop_reason):
        if reason in ERROR_CODES:
            return OUTCOMES[ERROR_CODES[reason]]
        if reason and (
            reason.endswith("_not_allowed")
            or reason
            in {
                "target_not_loopback",
                "target_url_invalid",
                "target_credentials_not_allowed",
            }
        ):
            return OUTCOMES["POLICY_REJECTED"]
    if status == "timed_out":
        return OUTCOMES["EXECUTION_TIMEOUT"]
    if status == "failed":
        return OUTCOMES["ASSERTION_FAILED"]
    if status in {"closed", "completed"} and not error_kind:
        return OUTCOMES["COMPLETED"]
    if status == "passed" and not error_kind:
        return OUTCOMES["PASSED"]
    return OUTCOMES["EXECUTION_ERROR"]


def transport_error_kind(error: BaseException) -> str:
    """按异常链类型识别 TLS，其他传输失败保留旧 request_failed 稳定码。

    仅遍历 cause/context 的对象类型，循环链安全终止，不读取或回显异常文本。
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ssl.SSLError):
            return "tls_error"
        error = error.__cause__ or error.__context__
    return "request_failed"


class OutcomeFields(BaseModel):
    """为公开结果模型增加只读派生字段，读取旧记录无需新增数据库列。"""

    @computed_field
    @property
    def result_code(self) -> str:
        """返回稳定业务结果码，依据当前状态派生，不缓存或修改存储字段。"""
        return outcome_for(
            getattr(self, "status", None),
            getattr(self, "error_kind", None),
            stop_reason=getattr(self, "stop_reason", None),
            ok=getattr(self, "ok", None),
        ).code

    @computed_field
    @property
    def result_status(self) -> int | None:
        """返回本项目结果语义码；运行中、取消与未知结果不伪造数字状态。"""
        return OUTCOMES[self.result_code].status


def result_fields(record: dict) -> dict:
    """复制运行记录并追加派生结果，保持冻结快照与磁盘正文不变。

    completed 仅说明编排结束；模型退化优先使用已有稳定原因，无法识别时
    保守标记 MODEL_ERROR，避免报告失败仍展示成测试通过。取消、总截止等
    生命周期终态继续优先于模型退化原因。
    """
    outcome = outcome_for(
        record.get("status"), record.get("error_kind"), stop_reason=record.get("stop_reason")
    )
    if record.get("status") == "completed" and (
        record.get("actual_llm_status") == "degraded" or record.get("degradation_reasons")
    ):
        reasons = [record.get("stop_reason"), *record.get("degradation_reasons", [])]
        known = next((reason for reason in reasons if reason in ERROR_CODES), None)
        outcome = outcome_for("error", known) if known else OUTCOMES["MODEL_ERROR"]
    return {**record, "result_code": outcome.code, "result_status": outcome.status}
