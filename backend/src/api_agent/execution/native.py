"""Native Python test execution engine via HTTPX in-memory runner."""

import asyncio
import json
import time
from typing import Any

import httpx

from api_agent.execution.checks import bounded_observation, json_equal
from api_agent.execution.request import build_request_headers, build_request_url
from api_agent.execution.schemas import (
    AssertionResult,
    CaseResult,
    ExecutionPlan,
    ResponseCapture,
    RunResult,
    unevaluated_assertions,
)
from api_agent.infrastructure.http import (
    MAX_BODY_CHARS,
    ResponseEncodingError,
    ResponseLimitError,
    bounded_send,
    decode_text,
    group_headers,
)
from api_agent.infrastructure.json_pointer import resolve as evaluate_pointer
from api_agent.infrastructure.outcomes import transport_error_kind
from api_agent.infrastructure.privacy import Context

# 哨兵：区分“断言没有可记录的观察值”与“观察值就是 None”。
_ABSENT = object()


class NativeHttpEngine:
    """纯 Python 原生执行驱动引擎。

    边界：
    1. 计划和结果在内存中处理，不需要 Node/Postman CLI 可执行文件；
    2. 网络访问依赖项目中的 httpx；实际耗时由目标、网络和超时配置决定；
    3. 支持完整的 5 种受控断言类型：status、content_type、response_time、
       json_equals、json_exists。
    """

    name = "native"

    def __init__(self, omit_response_payload: bool = False, max_response_bytes: int = 2_097_152):
        """保存捕获开关与响应字节预算；每次 run 自行创建并关闭 HTTP 客户端。"""
        self.omit_response_payload = omit_response_payload
        self.max_response_bytes = max_response_bytes

    async def status(self) -> dict[str, Any]:
        """原生引擎基于内置 httpx 实现，始终可用。"""
        return {
            "available": True,
            "engine": self.name,
            "version": "1.0.0",
            "format": "native-httpx",
        }

    async def run(self, plan: ExecutionPlan, secrets: dict[str, str]) -> RunResult:
        """纯内存执行一份受控执行计划，返回归一化后的运行结果。"""
        started = time.monotonic()
        results: list[CaseResult] = []

        try:
            async with asyncio.timeout(plan.timeout_ms / 1000):
                async with httpx.AsyncClient(
                    timeout=plan.request_timeout_ms / 1000,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    for index, case in enumerate(plan.requests):
                        if index > 0 and plan.delay_ms > 0:
                            await asyncio.sleep(plan.delay_ms / 1000)

                        case_result = await self._run_case(client, plan, case, secrets)
                        results.append(case_result)
        except TimeoutError:
            # 整体超时
            duration = round((time.monotonic() - started) * 1000, 2)
            finished_ids = {r.id for r in results}
            for case in plan.requests:
                if case.id not in finished_ids:
                    results.append(
                        CaseResult(
                            id=case.id, status="unknown", assertions=unevaluated_assertions(case)
                        )
                    )
            return RunResult(
                status="timed_out",
                error_kind="execution_timeout",
                engine=self.name,
                duration_ms=duration,
                reserved_requests=len(plan.requests),
                observed_attempts=len(finished_ids),
                unknown_requests=len(plan.requests) - len(finished_ids),
                executions=results,
            )
        except asyncio.CancelledError:
            duration = round((time.monotonic() - started) * 1000, 2)
            finished_ids = {r.id for r in results}
            for case in plan.requests:
                if case.id not in finished_ids:
                    results.append(
                        CaseResult(
                            id=case.id, status="unknown", assertions=unevaluated_assertions(case)
                        )
                    )
            return RunResult(
                status="cancelled",
                error_kind="cancelled",
                engine=self.name,
                duration_ms=duration,
                reserved_requests=len(plan.requests),
                observed_attempts=len(finished_ids),
                unknown_requests=len(plan.requests) - len(finished_ids),
                executions=results,
            )

        duration = round((time.monotonic() - started) * 1000, 2)
        has_error = any(r.status == "error" for r in results)
        has_timeout = any(r.status == "timed_out" for r in results)
        has_failed = any(r.status == "failed" for r in results)

        if has_error:
            overall_status = "error"
            error_kind = next(
                (r.error_kind for r in results if r.status == "error"), "request_error"
            )
        elif has_timeout:
            overall_status = "timed_out"
            error_kind = "request_timeout"
        elif has_failed:
            overall_status = "failed"
            error_kind = None
        else:
            overall_status = "passed"
            error_kind = None

        return RunResult(
            status=overall_status,
            error_kind=error_kind,
            engine=self.name,
            duration_ms=duration,
            reserved_requests=len(plan.requests),
            observed_attempts=len(results),
            unknown_requests=len(plan.requests) - len(results),
            executions=results,
        )

    async def _run_case(
        self,
        client: httpx.AsyncClient,
        plan: ExecutionPlan,
        case: Any,
        secrets: dict[str, str],
    ) -> CaseResult:
        """执行单个请求用例并比对断言。"""
        headers = build_request_headers(plan, case, secrets)
        # 明确声明不压缩：``bounded_send`` 在原始字节上记账，
        # 自动解压会让预算作用在已放大后的内容上。
        headers["Accept-Encoding"] = "identity"
        url = build_request_url(plan, case)
        content = (
            json.dumps(case.body, ensure_ascii=True).encode("utf-8")
            if case.body is not None
            else None
        )

        case_start = time.monotonic()
        try:
            request = client.build_request(case.method, url, headers=headers, content=content)
            # HTTPX 的读超时按片段重置；额外限制整次请求至完整正文的挂钟耗时。
            async with asyncio.timeout(plan.request_timeout_ms / 1000):
                response = await bounded_send(client, request, self.max_response_bytes)
            elapsed_ms = round((time.monotonic() - case_start) * 1000, 2)
        except (ResponseLimitError, ResponseEncodingError) as error:
            return CaseResult(
                id=case.id,
                status="error",
                error_kind=(
                    "response_limit_exceeded"
                    if isinstance(error, ResponseLimitError)
                    else "response_encoding_not_supported"
                    if str(error) == "response_encoding_not_supported"
                    else "response_decode_error"
                ),
                response_time_ms=round((time.monotonic() - case_start) * 1000, 2),
                assertions=unevaluated_assertions(case),
            )
        except (TimeoutError, httpx.TimeoutException):
            elapsed_ms = round((time.monotonic() - case_start) * 1000, 2)
            return CaseResult(
                id=case.id,
                status="timed_out",
                error_kind="request_timeout",
                response_time_ms=elapsed_ms,
                assertions=unevaluated_assertions(case),
            )
        except (httpx.HTTPError, OSError) as error:
            elapsed_ms = round((time.monotonic() - case_start) * 1000, 2)
            return CaseResult(
                id=case.id,
                status="error",
                error_kind=transport_error_kind(error),
                response_time_ms=elapsed_ms,
                assertions=unevaluated_assertions(case),
            )

        # 尝试解析 JSON 响应体用于 JSON 断言
        parsed_json = None
        has_parsed_json = False
        try:
            parsed_json = response.json()
            has_parsed_json = True
        except Exception:
            pass

        assertion_results: list[AssertionResult] = []
        context = Context(secrets.values())
        context.learn(plan.model_dump())
        if has_parsed_json:
            context.learn(parsed_json)
        for index, assertion in enumerate(case.assertions):
            passed, message, observed = self._evaluate_assertion(
                assertion, response, elapsed_ms, has_parsed_json, parsed_json
            )
            observation = {}
            if not self.omit_response_payload:
                if observed is not _ABSENT:
                    observation = bounded_observation(observed, assertion.pointer, context)
                elif assertion.kind == "status":
                    observation = bounded_observation(response.status_code, "", context)
                elif assertion.kind == "response_time":
                    observation = bounded_observation(elapsed_ms, "", context)
            assertion_results.append(
                AssertionResult(
                    index=index,
                    kind=assertion.kind,
                    passed=passed,
                    message=context.text(message) if message else None,
                    pointer=assertion.pointer,
                    **observation,
                )
            )

        case_passed = all(a.passed is True for a in assertion_results)
        case_status = "passed" if case_passed else "failed"

        # 构造响应捕获
        if self.omit_response_payload:
            response_capture = ResponseCapture(omitted=True)
        else:
            raw_content = response.content
            decoded_body, body_truncated = decode_text(raw_content, MAX_BODY_CHARS)
            response_capture = ResponseCapture(
                status_text=response.reason_phrase or None,
                headers=group_headers(response.headers.multi_items()),
                body=decoded_body,
                body_bytes=len(raw_content),
                body_truncated=body_truncated,
                omitted=False,
            )

        return CaseResult(
            id=case.id,
            status=case_status,
            http_status=response.status_code,
            response_time_ms=elapsed_ms,
            assertions=assertion_results,
            response=response_capture,
        )

    def _evaluate_assertion(
        self,
        assertion: Any,
        response: httpx.Response,
        elapsed_ms: float,
        has_parsed_json: bool,
        parsed_json: Any,
    ) -> tuple[bool, str | None, Any]:
        """比对一条断言，返回 ``(是否通过, 差异说明, 观察值)``。

        实现：JSON 断言在这一步就把指针解出来，并把它作为第三个返回值交给
        调用方记录证据——此前调用方会再求值一次同一指针，既浪费也让
        “判定用的值”和“记录的值”存在分叉风险。
        非 JSON 断言返回哨兵 ``_ABSENT``，由调用方按类型自行记录观察值。
        """
        kind = assertion.kind

        if kind == "status":
            if response.status_code == assertion.expected:
                return True, None, _ABSENT
            return (
                False,
                f"expected HTTP status {assertion.expected} but received {response.status_code}",
                _ABSENT,
            )

        if kind == "content_type":
            header_val = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if header_val == "application/json":
                return True, None, _ABSENT
            return (
                False,
                f"expected content-type 'application/json' but received '{header_val}'",
                _ABSENT,
            )

        if kind == "response_time":
            if elapsed_ms <= assertion.expected:
                return True, None, _ABSENT
            return (
                False,
                f"expected response time <= {assertion.expected}ms but took {elapsed_ms:.1f}ms",
                _ABSENT,
            )

        if kind in {"json_equals", "json_exists"}:
            if not has_parsed_json:
                return False, "response body could not be parsed as valid JSON", _ABSENT

            exists, value = evaluate_pointer(parsed_json, assertion.pointer)
            if kind == "json_exists":
                if exists:
                    return True, None, value
                return (
                    False,
                    f"JSON Pointer '{assertion.pointer}' does not exist in response",
                    _ABSENT,
                )

            if not exists:
                return (
                    False,
                    f"JSON Pointer '{assertion.pointer}' not found for equality comparison",
                    _ABSENT,
                )
            if json_equal(value, assertion.expected):
                return True, None, value
            if self.omit_response_payload:
                return (
                    False,
                    f"value at JSON Pointer '{assertion.pointer}' did not equal expected",
                    value,
                )
            return False, f"value at '{assertion.pointer}' did not match expected value", value

        return False, f"unknown assertion kind: {kind}", _ABSENT
