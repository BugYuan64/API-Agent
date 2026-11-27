"""Normalize the Postman CLI report into a bounded, inspectable result."""

import base64
import json
import math
from typing import Any

from api_agent.execution.checks import bounded_observation
from api_agent.execution.schemas import (
    AssertionResult,
    CaseResult,
    ExecutionPlan,
    ExtractedVariable,
    ResponseCapture,
    RunResult,
    empty_result,
    unevaluated_assertions,
)
from api_agent.execution.variables import extract_from_response, is_rule_sensitive
from api_agent.infrastructure.json_pointer import resolve as evaluate_pointer
from api_agent.infrastructure.logging import REDACTED
from api_agent.infrastructure.outcomes import outcome_for
from api_agent.infrastructure.privacy import Context

# 单条响应体最多保留的字符数；超出部分截断并置 body_truncated，
# 避免一个超大响应把整份结果撑到不可用。
MAX_BODY_CHARS = 1_000_000


def decode_stream(stream) -> tuple[str | None, int, bool]:
    """把 Postman 的 ``stream`` 字段还原成文本。"""
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
    """把响应头数组聚合成“名字 → 值列表”。"""
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
    """把一条 ``response`` 记录转成对外暴露的 ResponseCapture。"""
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


def _observe_assertion(
    wanted,
    *,
    code: int,
    elapsed: float | int | None,
    headers: dict[str, list[str]],
    has_parsed_json: bool,
    parsed_json: Any,
    context: Context,
) -> dict[str, Any]:
    """依据已捕获响应构造与原生引擎同构的有界脱敏断言观察值。"""
    if wanted.kind == "status":
        return bounded_observation(code, "", context)
    if wanted.kind == "response_time":
        if elapsed is not None:
            return bounded_observation(elapsed, "", context)
        return {}
    if wanted.kind == "content_type":
        raw_ct = next(
            (
                values[0]
                for name, values in headers.items()
                if name.lower() == "content-type" and values
            ),
            "",
        )
        return bounded_observation(raw_ct.split(";")[0].strip().lower(), "", context)
    if wanted.kind in {"json_equals", "json_exists"} and has_parsed_json:
        exists, value = evaluate_pointer(parsed_json, wanted.pointer)
        if exists:
            return bounded_observation(value, wanted.pointer, context)
    return {}


def normalize_report(
    report: dict,
    plan: ExecutionPlan,
    exit_code: int,
    capture: bool = True,
    *,
    secrets: set[str] | None = None,
) -> RunResult:
    """把 Postman CLI 的 JSON 报告归一成受控的 ``RunResult``（含断言观察值与变量提取回显）。"""
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
    secret_pool = set(secrets or ())
    for case in plan.requests:
        item = CaseResult(id=case.id, assertions=unevaluated_assertions(case))
        executions.append(item)
        entry = observed.get(case.id)
        if entry is None:
            continue
        response = entry.get("response")
        # 识别 prerequest 通过 pm.execution.skipRequest() 主动跳过的受控步骤
        if not entry.get("requestError") and not isinstance(response, dict):
            pre_assertions = entry.get("assertions")
            if (
                isinstance(pre_assertions, list)
                and len(pre_assertions) == 1
                and isinstance(pre_assertions[0], dict)
                and isinstance(pre_assertions[0].get("assertion"), str)
            ):
                marker = pre_assertions[0]["assertion"]
                if marker.startswith("__aa_skip:"):
                    reason = marker.removeprefix("__aa_skip:")
                    if reason in {"dependency_failed", "missing_variable"}:
                        item.status, item.error_kind = "skipped", reason
                        continue
                elif marker.startswith("__aa_error:"):
                    reason = marker.removeprefix("__aa_error:")
                    if reason in {"unsafe_variable_value"}:
                        item.status, item.error_kind = "error", reason
                        continue
        if entry.get("requestError") or not isinstance(response, dict):
            request_error = entry.get("requestError")
            error_code = request_error.get("code") if isinstance(request_error, dict) else None
            if isinstance(error_code, str) and error_code in {"ETIMEDOUT", "ESOCKETTIMEDOUT"}:
                item.status, item.error_kind = "timed_out", "request_timeout"
            else:
                item.status, item.error_kind = "error", "request_error"
            item.response = (
                ResponseCapture(body=outcome_for(item.status, item.error_kind).message)
                if capture
                else ResponseCapture(omitted=True)
            )
            continue
        code = response.get("code")
        if type(code) is not int or not 100 <= code <= 599:
            item.status, item.error_kind = "error", "invalid_response_metadata"
            continue
        item.http_status = code
        elapsed = response.get("responseTime")
        valid_elapsed: float | int | None = None
        if type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0:
            item.response_time_ms = elapsed
            valid_elapsed = elapsed
        raw_capture = capture_response(response)
        item.response = raw_capture if capture else ResponseCapture(omitted=True)
        raw_assertions = entry.get("assertions")
        expected_total = len(case.assertions) + len(case.extract)
        if not isinstance(raw_assertions, list) or len(raw_assertions) != expected_total:
            item.status, item.error_kind = "error", "missing_assertions"
            continue
        assertion_entries = raw_assertions[: len(case.assertions)]
        extract_entries = raw_assertions[len(case.assertions) :]

        parsed_json: Any = None
        has_parsed_json = False
        if not raw_capture.omitted and isinstance(raw_capture.body, str) and raw_capture.body:
            try:
                parsed_json = json.loads(raw_capture.body)
                has_parsed_json = True
            except ValueError:
                pass

        extracted_by_name: dict[str, dict[str, Any]] = {}
        if case.extract:
            _, raw_extracted, new_secrets, _ = extract_from_response(
                case.extract,
                status_code=code,
                headers=raw_capture.headers,
                has_parsed_json=has_parsed_json,
                parsed_json=parsed_json,
                omit_values=not capture,
            )
            secret_pool.update(new_secrets)
            extracted_by_name = {rec["name"]: rec for rec in raw_extracted}

        context = Context(secret_pool)
        context.learn(plan.model_dump())
        if has_parsed_json:
            context.learn(parsed_json)

        valid = True
        item.assertions = []
        for index, (actual, wanted) in enumerate(
            zip(assertion_entries, case.assertions, strict=True)
        ):
            if (
                not isinstance(actual, dict)
                or actual.get("assertion") != f"a{index}_{wanted.kind}"
                or actual.get("skipped")
            ):
                valid = False
                break
            passed = "error" not in actual
            observation = (
                _observe_assertion(
                    wanted,
                    code=code,
                    elapsed=valid_elapsed,
                    headers=raw_capture.headers,
                    has_parsed_json=has_parsed_json,
                    parsed_json=parsed_json,
                    context=context,
                )
                if capture
                else {}
            )
            raw_message = _assertion_message(actual) if capture and not passed else None
            item.assertions.append(
                AssertionResult(
                    index=index,
                    kind=wanted.kind,
                    pointer=wanted.pointer,
                    passed=passed,
                    message=context.text(raw_message) if raw_message else None,
                    **observation,
                )
            )

        required_extract_failed = False
        extracted_items: list[ExtractedVariable] = []
        if valid and case.extract:
            for ext_idx, (actual, rule) in enumerate(
                zip(extract_entries, case.extract, strict=True)
            ):
                if (
                    not isinstance(actual, dict)
                    or actual.get("assertion") != f"e{ext_idx}_{rule.name}"
                    or actual.get("skipped")
                ):
                    valid = False
                    break
                ok = "error" not in actual
                sensitive = is_rule_sensitive(rule)
                if not ok and rule.required:
                    required_extract_failed = True
                matched_extract = extracted_by_name.get(rule.name)
                if ok:
                    if sensitive or not capture:
                        value = REDACTED
                    elif matched_extract and matched_extract.get("ok"):
                        value = context.clean(matched_extract.get("value"))
                    else:
                        value = None
                else:
                    value = None
                extracted_items.append(
                    ExtractedVariable(
                        name=rule.name,
                        source=rule.source,
                        ok=ok,
                        sensitive=sensitive,
                        value=value,
                        message=None if ok else "extraction failed",
                    )
                )

        if not valid:
            item.status, item.error_kind = "error", "invalid_assertions"
        else:
            item.extracted = extracted_items
            assertions_ok = all(a.passed for a in item.assertions)
            if assertions_ok and not required_extract_failed:
                item.status = "passed"
            else:
                item.status = "failed"
                if assertions_ok and required_extract_failed:
                    item.error_kind = "variable_extraction_failed"
    result.executions = executions
    result.observed_attempts = sum(
        1 for item in executions if item.status not in {"unknown", "skipped"}
    )
    result.unknown_requests = sum(1 for item in executions if item.status == "unknown")
    if any(item.status == "unknown" for item in executions):
        result.error_kind = "incomplete_or_error_report"
    elif any(item.status == "error" for item in executions):
        result.error_kind = next(item.error_kind for item in executions if item.status == "error")
    elif any(item.status == "timed_out" for item in executions):
        result.status, result.error_kind = "timed_out", "request_timeout"
    elif any(item.status == "failed" for item in executions):
        result.status, result.error_kind = "failed", None
    elif any(item.status == "skipped" for item in executions):
        result.status = "failed"
        result.error_kind = next(
            (item.error_kind for item in executions if item.status == "skipped"),
            "dependency_failed",
        )
    elif exit_code != 0 or run.get("error") or run.get("failures"):
        result.error_kind = "cli_error"
    else:
        result.status, result.error_kind = "passed", None
    return result


def _assertion_message(actual: dict) -> str | None:
    """取出断言失败时引擎给出的差异说明。"""
    error = actual.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return None
