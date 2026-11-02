"""Normalize the Postman CLI report into a bounded, inspectable result."""

import base64
import math

from api_agent.execution.schemas import (
    AssertionResult,
    CaseResult,
    ExecutionPlan,
    ResponseCapture,
    RunResult,
    empty_result,
)

# 单条响应体最多保留的字符数；超出部分截断并置 body_truncated，
# 避免一个超大响应把整份结果撑到不可用。
MAX_BODY_CHARS = 1_000_000


def decode_stream(stream) -> tuple[str | None, int, bool]:
    """把 Postman 的 ``stream`` 字段还原成文本。

    作用：引擎把响应体序列化成 ``{"type":"Buffer","data":[123,34,...]}``
    （Node 的 Buffer JSON 形式），也可能是 base64 字符串或纯文本，
    这里统一还原成可读文本。
    实现：按三种形态分别处理——Buffer 数组逐字节还原、
    带 ``base64`` 标记或疑似 base64 的字符串先解码再按 UTF-8 容错解码、
    其余直接当文本。任何解码失败都返回 ``(None, 长度, True)``，
    让上层知道“有响应体但没法按文本展示”，而不是伪装成空响应。
    """
    if stream is None:
        return None, 0, False
    data: bytes
    if isinstance(stream, dict) and stream.get("type") == "Buffer":
        raw = stream.get("data")
        if not isinstance(raw, list):
            return None, 0, True
        try:
            data = bytes(byte & 0xFF for byte in raw if isinstance(byte, int))
        except (TypeError, ValueError):
            return None, 0, True
    elif isinstance(stream, dict) and isinstance(stream.get("data"), str):
        data = _decode_base64(stream["data"])
    elif isinstance(stream, str):
        # 引擎早期版本直接给 base64 文本；能干净解码就按 base64 处理，
        # 否则当普通文本，避免把正常文本误伤成乱码。
        decoded = _decode_base64(stream, strict=True)
        data = decoded if decoded is not None else stream.encode("utf-8", "replace")
    else:
        return None, 0, True
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS], len(data), True
    return text, len(data), False


def _decode_base64(value: str, strict: bool = False) -> bytes | None:
    """把 base64 字符串解码成字节；不合法时按 ``strict`` 决定返回 None 还是原文。"""
    try:
        return base64.b64decode("".join(value.split()), validate=True)
    except (ValueError, TypeError):
        return None if strict else value.encode("utf-8", "replace")


def capture_headers(raw) -> dict[str, list[str]]:
    """把响应头数组聚合成“名字 → 值列表”。

    作用：引擎给的是 ``[{"key":"X","value":"1"}, ...]``，同名头可能出现多次
    （如多个 Set-Cookie），用列表保留全部值，避免后一个覆盖前一个。
    实现：遍历列表，只接受 ``key`` 为非空字符串的条目；
    键名保持原样不做大小写归一，让用户看到目标真实发送的写法。
    """
    headers: dict[str, list[str]] = {}
    if not isinstance(raw, list):
        return headers
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        headers.setdefault(key, []).append(str(item.get("value", "")))
    return headers


def capture_response(response: dict) -> ResponseCapture:
    """把一条 ``response`` 记录转成对外暴露的 ResponseCapture。

    实现：头与体都从记录里取；若报告被 ``--omitAllHeadersAndBody`` 裁过，
    两个字段都会缺失，此时 ``omitted`` 置 True，让调用方明确知道
    “不是目标没返回，而是本次运行关掉了捕获”。
    """
    has_header = "header" in response
    has_stream = "stream" in response
    if not has_header and not has_stream:
        return ResponseCapture(omitted=True)
    body, size, truncated = decode_stream(response.get("stream"))
    status_text = response.get("status")
    return ResponseCapture(
        status_text=status_text if isinstance(status_text, str) else None,
        headers=capture_headers(response.get("header")),
        body=body,
        body_bytes=size,
        body_truncated=truncated,
    )


def normalize_report(
    report: dict,
    plan: ExecutionPlan,
    exit_code: int,
    capture: bool = True,
) -> RunResult:
    """把 Postman CLI 的 JSON 报告归一成受控的 ``RunResult``。

    作用：从引擎报告里提取“哪些用例跑了、HTTP 状态码、耗时、每条断言结果”，
    并在 ``capture`` 为真时一并带出目标实际返回的头与体，便于人工核对。
    实现步骤：
    ① 先假设结果为 ``invalid_report``，只有结构完全符合预期才继续；
    ② 校验 ``run.executions`` 是列表，逐条要求 ``item.name`` 命中计划里的 ID
       且不得重复——出现未知或重复条目就整体判为无效报告，
       因为无法再保证“结果与计划一一对应”；
    ③ 统计 observed/unknown，使上层能看出有多少请求根本没有回音；
    ④ 按计划顺序遍历用例：缺条目保持 unknown；有 ``requestError`` 记为 request_error；
       状态码必须是 100..599 的真实 int；耗时必须是有限非负数才采信；
       断言数组长度必须与计划完全一致，且每条断言名要对上 ``a{下标}_{类型}``、
       不能被标记 skipped，否则判为 invalid_assertions；
    ⑤ 最后按优先级定总体状态：存在 unknown/error → 报告不完整；
       有用例 failed → 整体 failed；CLI 退出码非 0 或报告里有 error/failures → cli_error；
       全部通过才置 passed。

    ``capture=False`` 时不读取响应头与响应体，但仍在每条结果上挂一个
    ``omitted`` 标记，让调用方能区分“本次运行关掉了捕获”和“没有结果”。
    """
    result = empty_result(plan, "error", "invalid_report")
    run = report.get("run")
    if not isinstance(run, dict) or not isinstance(run.get("executions"), list):
        return result
    entries = run["executions"]
    expected = {case.id: case for case in plan.requests}
    observed = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("item"), dict):
            return result
        case_id = entry["item"].get("name")
        if not isinstance(case_id, str) or case_id not in expected or case_id in observed:
            return result
        observed[case_id] = entry
    result.observed_attempts = len(observed)
    result.unknown_requests = len(expected) - len(observed)
    executions = []
    for case in plan.requests:
        item = CaseResult(id=case.id)
        executions.append(item)
        entry = observed.get(case.id)
        if entry is None:
            continue
        response = entry.get("response")
        if entry.get("requestError") or not isinstance(response, dict):
            item.status, item.error_kind = "error", "request_error"
            item.response = (
                ResponseCapture(body=_error_text(entry.get("requestError")))
                if capture
                else ResponseCapture(omitted=True)
            )
            continue
        code = response.get("code")
        # 用 type(...) is int 而非 isinstance，排除 True/False 被当成 1/0 的情况。
        if type(code) is not int or not 100 <= code <= 599:
            item.status, item.error_kind = "error", "invalid_response_metadata"
            continue
        item.http_status = code
        elapsed = response.get("responseTime")
        if type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0:
            item.response_time_ms = elapsed
        item.response = capture_response(response) if capture else ResponseCapture(omitted=True)
        assertions = entry.get("assertions")
        if not isinstance(assertions, list) or len(assertions) != len(case.assertions):
            item.status, item.error_kind = "error", "missing_assertions"
            continue
        valid = True
        for index, (actual, wanted) in enumerate(zip(assertions, case.assertions, strict=True)):
            if (
                not isinstance(actual, dict)
                or actual.get("assertion") != f"a{index}_{wanted.kind}"
                or actual.get("skipped")
            ):
                valid = False
                break
            passed = "error" not in actual
            item.assertions.append(
                AssertionResult(
                    index=index,
                    kind=wanted.kind,
                    passed=passed,
                    message=_assertion_message(actual) if capture and not passed else None,
                )
            )
        if not valid:
            item.status, item.error_kind = "error", "invalid_assertions"
        else:
            item.status = "passed" if all(a.passed for a in item.assertions) else "failed"
    result.executions = executions
    if any(item.status in {"unknown", "error"} for item in executions):
        result.error_kind = "incomplete_or_error_report"
    elif any(item.status == "failed" for item in executions):
        result.status, result.error_kind = "failed", None
    elif exit_code != 0 or run.get("error") or run.get("failures"):
        result.error_kind = "cli_error"
    else:
        result.status, result.error_kind = "passed", None
    return result


def _assertion_message(actual: dict) -> str | None:
    """取出断言失败时引擎给出的差异说明。

    实现：报告里形如 ``{"error": {"message": "expected 'x' to equal 'y'"}}``；
    只接受非空字符串，其余返回 None，避免把结构异常的内容当文案透出。
    """
    error = actual.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return None


def _error_text(raw) -> str | None:
    """取出请求级错误（连接失败、超时等）的可读说明。"""
    if isinstance(raw, dict):
        message = raw.get("message")
        if isinstance(message, str) and message:
            return message
    return None
