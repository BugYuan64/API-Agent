"""Native Python test execution engine via HTTPX in-memory runner (supports stateful chains)."""

import asyncio
import json
import time
from contextvars import ContextVar
from typing import Any

import httpx

from api_agent.execution.checks import bounded_observation, json_equal
from api_agent.execution.request import build_request_headers, build_request_url
from api_agent.execution.schemas import (
    AssertionResult,
    CaseResult,
    ExecutionPlan,
    ExtractedVariable,
    ResponseCapture,
    RunResult,
    unevaluated_assertions,
)
from api_agent.execution.variables import (
    extract_from_response,
    is_safe_header_value,
    is_safe_resolved_path,
    resolve_json_value,
    resolve_string,
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
# 协程级上下文隔离单次 run 的变量池与敏感值集合，既并发安全又保持 _run_case 四参签名向后兼容。
_RUN_VARS: ContextVar[dict[str, Any] | None] = ContextVar("_RUN_VARS", default=None)
_RUN_SECRETS: ContextVar[set[str] | None] = ContextVar("_RUN_SECRETS", default=None)


class NativeHttpEngine:
    """纯 Python 原生执行驱动引擎（支持有状态多步骤变量链、依赖控制与清理步骤）。

    边界：
    1. 计划和结果在内存中处理，不需要 Node/Postman CLI 可执行文件；
    2. 网络访问依赖项目中的 httpx；实际耗时由目标、网络和超时配置决定；
    3. 支持完整的 5 种受控断言类型及 JSON Pointer / 响应头 / 状态码变量提取与注入。
    """

    name = "native"

    def __init__(
        self,
        omit_response_payload: bool = False,
        max_response_bytes: int = 2_097_152,
        *,
        trust_env: bool = False,
    ):
        """保存捕获开关、响应字节预算与代理策略；每次 run 自行创建并关闭 HTTP 客户端。"""
        self.omit_response_payload = omit_response_payload
        self.max_response_bytes = max_response_bytes
        self.trust_env = trust_env

    async def status(self) -> dict[str, Any]:
        """原生引擎基于内置 httpx 实现，始终可用。"""
        return {
            "available": True,
            "engine": self.name,
            "version": "1.1.0",
            "format": "native-httpx",
        }

    async def run(self, plan: ExecutionPlan, secrets: dict[str, str]) -> RunResult:
        """纯内存执行一份受控执行计划，返回归一化后的运行结果。"""
        started = time.monotonic()
        results: list[CaseResult] = []
        runtime_vars: dict[str, Any] = dict(plan.variables)
        runtime_secrets: set[str] = set(secrets.values())
        case_outcomes: dict[str, str] = {}
        vars_token = _RUN_VARS.set(runtime_vars)
        secrets_token = _RUN_SECRETS.set(runtime_secrets)

        try:
            async with asyncio.timeout(plan.timeout_ms / 1000):
                async with httpx.AsyncClient(
                    timeout=plan.request_timeout_ms / 1000,
                    follow_redirects=False,
                    trust_env=self.trust_env,
                ) as client:
                    for index, case in enumerate(plan.requests):
                        if index > 0 and plan.delay_ms > 0:
                            await asyncio.sleep(plan.delay_ms / 1000)

                        if (
                            case.depends_on
                            and not case.always_run
                            and any(case_outcomes.get(dep) != "passed" for dep in case.depends_on)
                        ):
                            case_result = CaseResult(
                                id=case.id,
                                status="skipped",
                                error_kind="dependency_failed",
                                assertions=unevaluated_assertions(case),
                            )
                        else:
                            case_result = await self._run_case(client, plan, case, secrets)
                        case_outcomes[case.id] = case_result.status
                        results.append(case_result)
        except TimeoutError:
            duration = round((time.monotonic() - started) * 1000, 2)
            finished_ids = {r.id for r in results}
            for case in plan.requests:
                if case.id not in finished_ids:
                    results.append(
                        CaseResult(
                            id=case.id, status="unknown", assertions=unevaluated_assertions(case)
                        )
                    )
            attempted = sum(1 for r in results if r.status not in {"unknown", "skipped"})
            unknown = sum(1 for r in results if r.status == "unknown")
            return RunResult(
                status="timed_out",
                error_kind="execution_timeout",
                engine=self.name,
                duration_ms=duration,
                reserved_requests=len(plan.requests),
                observed_attempts=attempted,
                unknown_requests=unknown,
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
            attempted = sum(1 for r in results if r.status not in {"unknown", "skipped"})
            unknown = sum(1 for r in results if r.status == "unknown")
            return RunResult(
                status="cancelled",
                error_kind="cancelled",
                engine=self.name,
                duration_ms=duration,
                reserved_requests=len(plan.requests),
                observed_attempts=attempted,
                unknown_requests=unknown,
                executions=results,
            )
        finally:
            _RUN_VARS.reset(vars_token)
            _RUN_SECRETS.reset(secrets_token)

        duration = round((time.monotonic() - started) * 1000, 2)
        has_error = any(r.status == "error" for r in results)
        has_timeout = any(r.status == "timed_out" for r in results)
        has_failed = any(r.status == "failed" for r in results)
        has_skipped = any(r.status == "skipped" for r in results)

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
        elif has_skipped:
            overall_status = "failed"
            error_kind = next(
                (r.error_kind for r in results if r.status == "skipped"), "dependency_failed"
            )
        else:
            overall_status = "passed"
            error_kind = None

        attempted = sum(1 for r in results if r.status not in {"unknown", "skipped"})
        unknown = sum(1 for r in results if r.status == "unknown")
        return RunResult(
            status=overall_status,
            error_kind=error_kind,
            engine=self.name,
            duration_ms=duration,
            reserved_requests=len(plan.requests),
            observed_attempts=attempted,
            unknown_requests=unknown,
            executions=results,
        )

    async def _run_case(
        self,
        client: httpx.AsyncClient,
        plan: ExecutionPlan,
        case: Any,
        secrets: dict[str, str],
        *,
        runtime_vars: dict[str, Any] | None = None,
        runtime_secrets: set[str] | None = None,
    ) -> CaseResult:
        """解析受控变量模板、执行单个请求用例、比对断言并提取变量。"""
        active_vars = runtime_vars if runtime_vars is not None else _RUN_VARS.get()
        variables = active_vars if active_vars is not None else dict(plan.variables)
        active_secrets = runtime_secrets if runtime_secrets is not None else _RUN_SECRETS.get()
        secret_pool = active_secrets if active_secrets is not None else set(secrets.values())

        # 1) 解析路径、查询串、请求头与请求体中的受控占位符
        resolved_path, path_missing = resolve_string(case.path, variables, preserve_scalar=False)
        missing_vars = list(path_missing)

        resolved_query: dict[str, str] = {}
        for q_key, q_val in case.query.items():
            r_val, q_miss = resolve_string(q_val, variables, preserve_scalar=False)
            resolved_query[q_key] = str(r_val)
            missing_vars.extend(q_miss)

        resolved_headers_raw: dict[str, str] = {}
        for h_key, h_val in case.headers.items():
            r_val, h_miss = resolve_string(h_val, variables, preserve_scalar=False)
            resolved_headers_raw[h_key] = str(r_val)
            missing_vars.extend(h_miss)

        resolved_body = None
        if case.body is not None:
            resolved_body, b_miss = resolve_json_value(case.body, variables)
            missing_vars.extend(b_miss)

        if missing_vars:
            return CaseResult(
                id=case.id,
                status="skipped",
                error_kind="missing_variable",
                assertions=unevaluated_assertions(case),
            )

        # 2) 运行期安全复核：防止上游返回恶意值触发路径穿越或头注入
        if not is_safe_resolved_path(resolved_path) or not plan.matches_path(
            resolved_path, allow_vars=False
        ):
            return CaseResult(
                id=case.id,
                status="error",
                error_kind="unsafe_variable_value",
                assertions=unevaluated_assertions(case),
            )
        if any(not is_safe_header_value(val) for val in resolved_headers_raw.values()):
            return CaseResult(
                id=case.id,
                status="error",
                error_kind="unsafe_variable_value",
                assertions=unevaluated_assertions(case),
            )

        headers = build_request_headers(plan, case, secrets, resolved_headers=resolved_headers_raw)
        # 明确声明不压缩：``bounded_send`` 在原始字节上记账，
        # 自动解压会让预算作用在已放大后的内容上。
        headers["Accept-Encoding"] = "identity"
        url = build_request_url(
            plan, case, resolved_path=resolved_path, resolved_query=resolved_query
        )
        content = (
            json.dumps(resolved_body, ensure_ascii=True).encode("utf-8")
            if resolved_body is not None
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

        # 3) 解析 JSON 响应体
        parsed_json = None
        has_parsed_json = False
        try:
            parsed_json = response.json()
            has_parsed_json = True
        except Exception:
            pass

        grouped_headers = group_headers(response.headers.multi_items())

        # 4) 执行变量提取并把敏感提取值登记到隐私上下文
        extracted_records: list[ExtractedVariable] = []
        extraction_failed = False
        if case.extract:
            new_vars, raw_records, new_secrets, extraction_failed = extract_from_response(
                case.extract,
                status_code=response.status_code,
                headers=grouped_headers,
                has_parsed_json=has_parsed_json,
                parsed_json=parsed_json,
                omit_values=self.omit_response_payload,
            )
            variables.update(new_vars)
            secret_pool.update(new_secrets)
            extracted_records = [ExtractedVariable.model_validate(rec) for rec in raw_records]

        # 5) 评估断言（支持在 json_equals 期望值中引用已绑定变量）
        assertion_results: list[AssertionResult] = []
        context = Context(secret_pool)
        context.learn(plan.model_dump())
        if has_parsed_json:
            context.learn(parsed_json)
        for index, assertion in enumerate(case.assertions):
            expected_val = assertion.expected
            if assertion.kind == "json_equals":
                expected_val, exp_missing = resolve_json_value(assertion.expected, variables)
                if exp_missing:
                    assertion_results.append(
                        AssertionResult(
                            index=index,
                            kind=assertion.kind,
                            passed=False,
                            message="Expected value references an unavailable variable",
                            pointer=assertion.pointer,
                        )
                    )
                    continue
            passed, message, observed = self._evaluate_assertion(
                assertion, expected_val, response, elapsed_ms, has_parsed_json, parsed_json
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

        case_passed = all(a.passed is True for a in assertion_results) and not extraction_failed
        case_status = "passed" if case_passed else "failed"
        case_error_kind = (
            "variable_extraction_failed"
            if extraction_failed and all(a.passed is True for a in assertion_results)
            else None
        )

        # 6) 构造响应捕获
        if self.omit_response_payload:
            response_capture = ResponseCapture(omitted=True)
        else:
            raw_content = response.content
            decoded_body, body_truncated = decode_text(raw_content, MAX_BODY_CHARS)
            response_capture = ResponseCapture(
                status_text=response.reason_phrase or None,
                headers=grouped_headers,
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
            error_kind=case_error_kind,
            assertions=assertion_results,
            extracted=extracted_records,
            response=response_capture,
        )

    def _evaluate_assertion(
        self,
        assertion: Any,
        expected_value: Any,
        response: httpx.Response,
        elapsed_ms: float,
        has_parsed_json: bool,
        parsed_json: Any,
    ) -> tuple[bool, str | None, Any]:
        """比对一条断言，返回 ``(是否通过, 差异说明, 观察值)``。"""
        kind = assertion.kind

        if kind == "status":
            if response.status_code == expected_value:
                return True, None, _ABSENT
            return (
                False,
                f"expected HTTP status {expected_value} but received {response.status_code}",
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
            if elapsed_ms <= expected_value:
                return True, None, _ABSENT
            return (
                False,
                f"expected response time <= {expected_value}ms but took {elapsed_ms:.1f}ms",
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
            if json_equal(value, expected_value):
                return True, None, value
            if self.omit_response_payload:
                return (
                    False,
                    f"value at JSON Pointer '{assertion.pointer}' did not equal expected",
                    value,
                )
            return False, f"value at '{assertion.pointer}' did not match expected value", value

        return False, f"unknown assertion kind: {kind}", _ABSENT
