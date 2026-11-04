"""AI suggests data; local code retains validation, evidence and budget authority."""

import json
import os
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field

from pydantic import ValidationError

from api_agent.agent.prompts import COMMON, STAGES
from api_agent.agent.schemas import (
    Adaptation,
    AdaptRequest,
    Analysis,
    DiagnoseRequest,
    Diagnosis,
    Evidence,
    GeneratedCases,
    GenerateRequest,
    Generation,
    Mutation,
    Proposal,
    ReportCommentary,
)
from api_agent.execution.schemas import RequestCase
from api_agent.execution.service import ExecutionService, PlanError
from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.json_pointer import is_valid as is_valid_pointer
from api_agent.infrastructure.json_pointer import parse as pointer_segments
from api_agent.infrastructure.privacy import Context
from api_agent.llm.client import (
    LLMAuthenticationError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMTimeoutError,
)
from api_agent.llm.schemas import ChatCompletionRequest, ChatMessage


class AgentError(ValueError):
    """A stable safe code, never raw supplier content."""


# 本地校验拒绝时使用的稳定代码：这些是“模型输出合法但未通过本地约束”，
# 与“模型输出不是合法 JSON”的 invalid_model_output 分开记录，便于定位。
# unknown_evidence 会带 `:id1,id2` 后缀，按前缀匹配。
RECOGNIZED_REJECTIONS = frozenset(
    {
        "duplicate_hypothesis",
        "request_budget_exceeded",
        "intent_case_mismatch",
        "invalid_contract_quote",
        "credential_echo_in_output",
        "incomplete_output",
        "output_too_large",
    }
)


def is_recognized_rejection(reason: str) -> bool:
    return reason in RECOGNIZED_REJECTIONS or reason.startswith("unknown_evidence")


@dataclass
class ModelCall:
    stage: str
    status: str = "dispatching"
    model: str = ""
    error_kind: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_reported: bool = False
    duration_ms: float = 0


@dataclass
class CallBudget:
    limit: int
    reserved: int = 0
    consumed: int = 0
    calls: list[ModelCall] = field(default_factory=list)
    changed: Callable[[], Awaitable[None]] | None = None

    @property
    def remaining(self):
        return self.limit - self.reserved - self.consumed

    @property
    def prompt_tokens(self):
        return sum(call.prompt_tokens for call in self.calls)

    async def notify(self):
        if self.changed:
            await self.changed()


def fingerprint(case: RequestCase) -> str:
    # Assertions and IDs do not make re-sending the same HTTP request new coverage.
    data = case.model_dump(exclude={"id", "assertions"})
    return json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False)


def evidence_for(request: DiagnoseRequest, context: Context) -> list[Evidence]:
    cases = {case.id: case for case in request.plan.requests}
    results = request.result.executions
    if len(results) != len(cases) or {result.id for result in results} != cases.keys():
        raise AgentError("result_plan_mismatch")
    evidence = []
    for result in results:
        prefix = f"r{request.round_index}:{result.id}"
        evidence.append(
            Evidence(
                id=prefix,
                case_id=result.id,
                status=result.status,
                http_status=result.http_status,
                error_kind=result.error_kind,
            )
        )
        seen = set()
        for assertion in result.assertions:
            if assertion.index in seen or not 0 <= assertion.index < len(
                cases[result.id].assertions
            ):
                raise AgentError("result_plan_mismatch")
            expected = cases[result.id].assertions[assertion.index]
            if expected.kind != assertion.kind:
                raise AgentError("result_plan_mismatch")
            seen.add(assertion.index)
            evidence.append(
                Evidence(
                    id=f"{prefix}:a{assertion.index}",
                    case_id=result.id,
                    status=result.status,
                    http_status=result.http_status,
                    error_kind=result.error_kind,
                    assertion_index=assertion.index,
                    kind=assertion.kind,
                    expected=context.clean(expected.expected),
                    passed=assertion.passed,
                    message=context.text(assertion.message) if assertion.message else None,
                    # 携带实际观测值与类型：没有它，模型无法区分“值不符”与“类型不符”。
                    # 执行层已脱敏并截断；截断时 actual 为 None 但 actual_present=True。
                    actual=context.clean(assertion.actual),
                    actual_present=assertion.actual_present,
                    actual_type=assertion.actual_type,
                    actual_truncated=assertion.actual_truncated,
                )
            )
        for index, assertion in enumerate(cases[result.id].assertions):
            if index not in seen:
                evidence.append(
                    Evidence(
                        id=f"{prefix}:a{index}",
                        case_id=result.id,
                        status="unknown",
                        http_status=result.http_status,
                        error_kind="missing_assertion",
                        assertion_index=index,
                        kind=assertion.kind,
                        expected=context.clean(assertion.expected),
                    )
                )
    return evidence


class AgentService:
    def __init__(self, settings: Settings, client):
        self.settings = settings
        self.client = client
        self.execution = ExecutionService(settings)
        secrets = [getattr(client, "api_key", None)]
        secrets.extend(os.environ.get(name) for name in settings.target_auth_env_vars)
        self.context = Context(value for value in secrets if value)

    async def structured(self, stage, data, output_type, budget, validate=None):
        """One initial attempt and at most one schema repair, both charged."""
        self.context.learn(data)
        context = self.context.clean(data)
        # Contract schema property definitions must not be mistaken for credentials.
        if "contract" in data:
            context["contract"] = self.context.contract(data["contract"])
        serialized = json.dumps(context, ensure_ascii=False, allow_nan=False)
        if len(serialized.encode()) > 196_608:
            raise AgentError("model_context_too_large")
        system = (
            COMMON
            + "\n"
            + STAGES[stage]
            + "\nJSON Schema:\n"
            + json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        )
        repair = ""
        for _ in range(2):
            if budget.remaining <= 0:
                raise AgentError("model_budget_exhausted")
            call = ModelCall(stage=stage, model=getattr(self.client, "model", "unknown"))
            budget.calls.append(call)
            budget.reserved += 1
            await budget.notify()  # Durable reservation precedes actual transport.
            started = time.monotonic()
            try:
                response = await self.client.chat_completion(
                    ChatCompletionRequest(
                        messages=[
                            ChatMessage(role="system", content=system),
                            ChatMessage(role="user", content=serialized + repair),
                        ],
                        temperature=0.2,
                        max_tokens=6000,
                    )
                )
                call.model = response.model
                call.usage_reported = bool(response.usage)
                call.prompt_tokens = max(0, response.usage.get("prompt_tokens", 0))
                call.completion_tokens = max(0, response.usage.get("completion_tokens", 0))
                call.status = "received"
            except LLMError as error:
                code = self._model_error(error)
                call.error_kind = code
                call.status = "error"
                raise AgentError(code) from None
            except (ValidationError, TypeError, ValueError, AttributeError):
                call.error_kind = "model_response_error"
                call.status = "error"
                raise AgentError("model_response_error") from None
            finally:
                call.duration_ms = round((time.monotonic() - started) * 1000, 3)
                budget.reserved -= 1
                budget.consumed += 1
                if call.status == "dispatching":
                    call.status = "unknown"
                await budget.notify()
            try:
                if response.finish_reason in {"length", "content_filter"}:
                    raise ValueError("incomplete_output")
                content = response.content or ""
                if len(content.encode()) > 262_144:
                    raise ValueError("output_too_large")
                content = content.strip()
                if content.startswith("```json\n") and content.endswith("```"):
                    content = content[8:-3].strip()
                output = output_type.model_validate_json(content)
                decoded = json.dumps(output.model_dump(), ensure_ascii=False)
                if any(secret in decoded for secret in self.context.redactor.secrets):
                    raise ValueError("credential_echo_in_output")
                if validate:
                    validate(output)
                call.status = "accepted"
                await budget.notify()
                return output
            except (ValueError, TypeError, RecursionError, ValidationError) as error:
                call.status = "rejected"
                # 区分“模型输出结构非法”与“本地校验拒绝”（如证据引用不存在）：
                # 两者都记为 invalid_model_output 会让排查无法定位，调用方也看不出
                # 该重试模型还是该放宽校验。已知的校验类 ValueError 直接沿用其稳定代码。
                reason = str(error)
                call.error_kind = (
                    reason
                    if isinstance(error, AgentError) or is_recognized_rejection(reason)
                    else "invalid_model_output"
                )
                await budget.notify()
                # Do not echo arbitrary output/error values into the repair prompt.
                repair = "\n上次输出未通过 JSON/范围/引用校验，请仅依据原始输入重新生成合法 JSON。"
        raise AgentError("invalid_model_output")

    @staticmethod
    def _model_error(error):
        for kind, code in (
            (LLMTimeoutError, "model_timeout"),
            (LLMConfigError, "model_not_configured"),
            (LLMAuthenticationError, "model_authentication_failed"),
            (LLMConnectionError, "model_connection_failed"),
        ):
            if isinstance(error, kind):
                return code
        return "model_response_error"

    async def generate(self, request: GenerateRequest, budget: CallBudget | None = None):
        budget = budget if budget is not None else CallBudget(2)
        if request.scope.origin not in self.settings.target_allowed_origins:
            raise AgentError("origin_not_allowed")
        if (
            request.scope.auth_ref
            and request.scope.auth_ref.env_var not in self.settings.target_auth_env_vars
        ):
            raise AgentError("auth_reference_not_allowed")
        contract = self.context.contract(request.contract)
        contract_text = (
            contract if isinstance(contract, str) else json.dumps(contract, ensure_ascii=False)
        )

        def validate(output):
            if len(output.requests) > request.max_cases:
                raise ValueError("request_budget_exceeded")
            ids = {case.id for case in output.requests}
            if len(output.intents) != len(ids) or {i.case_id for i in output.intents} != ids:
                raise ValueError("intent_case_mismatch")
            for intent in output.intents:
                if intent.basis == "contract" and (
                    not intent.source_quote or intent.source_quote not in contract_text
                ):
                    raise ValueError("invalid_contract_quote")
            self.execution.parse(request.scope.plan(output.requests).model_dump())

        output = await self.structured(
            "generate",
            {
                "contract": request.contract,
                "scope": request.scope.model_dump(),
                "max_cases": request.max_cases,
            },
            GeneratedCases,
            budget,
            validate,
        )
        notes = output.notes
        if request.scope.auth_ref:
            notes = [*notes, "本计划统一携带鉴权；无 token 场景请另建 auth_ref=null 的计划。"]
        return Generation(
            plan=request.scope.plan(output.requests), intents=output.intents, notes=notes
        )

    async def diagnose(self, request: DiagnoseRequest, budget: CallBudget | None = None):
        self.context.learn(request.plan.model_dump())
        evidence = evidence_for(request, self.context)
        valid_ids = {item.id for item in evidence}

        def validate(output):
            if len({h.id for h in output.hypotheses}) != len(output.hypotheses):
                raise ValueError("duplicate_hypothesis")
            unknown = {evid for h in output.hypotheses for evid in h.evidence_ids} - valid_ids
            if unknown:
                # 带出被拒的引用 id，便于区分“模型编造证据”与“证据集本身有误”。
                raise ValueError(f"unknown_evidence:{','.join(sorted(unknown)[:3])}")

        analysis = await self.structured(
            "diagnose",
            {
                "contract": request.contract,
                "plan": request.plan.model_dump(),
                "evidence": [item.model_dump() for item in evidence],
            },
            Analysis,
            budget if budget is not None else CallBudget(2),
            validate,
        )
        return Diagnosis(evidence=evidence, analysis=analysis)

    async def adapt(self, request: AdaptRequest, budget: CallBudget | None = None):
        self.execution.parse(request.plan.model_dump())
        if not request.remaining_requests or not request.diagnosis.analysis.hypotheses:
            return Adaptation(continue_testing=False, reason="无剩余请求预算或无待验证假设。")
        proposal = await self.structured(
            "adapt", request.model_dump(), Proposal, budget if budget is not None else CallBudget(2)
        )
        if not proposal.continue_testing:
            return Adaptation(continue_testing=False, reason=proposal.reason)
        parents = {case.id: case for case in request.plan.requests}
        hypotheses = {h.id for h in request.diagnosis.analysis.hypotheses}
        seen = {fingerprint(case) for case in [*parents.values(), *request.previous_requests]}
        accepted, mutations, rejected = [], [], []
        for mutation in proposal.mutations:
            try:
                if len(accepted) >= min(request.remaining_requests, 50):
                    raise AgentError("request_budget_exhausted")
                parent = parents.get(mutation.parent_id)
                if parent is None:
                    raise AgentError("unknown_parent")
                if mutation.hypothesis_id not in hypotheses:
                    raise AgentError("unknown_hypothesis")
                if (
                    parent.method not in {"GET", "HEAD", "OPTIONS"}
                    and not request.allow_mutating_followups
                ):
                    raise AgentError("mutating_followup_not_allowed")
                case = apply_mutation(parent, mutation, f"r{request.round_index}-m{len(accepted)}")
                self.execution.parse(
                    request.plan.model_copy(
                        update={
                            "requests": [case],
                            "max_requests": 1,
                        }
                    ).model_dump()
                )
                key = fingerprint(case)
                if key in seen:
                    raise AgentError("duplicate_request")
                seen.add(key)
                accepted.append(case)
                mutations.append(mutation)
            except (AgentError, PlanError, ValidationError, TypeError, ValueError) as error:
                rejected.append(
                    str(error) if isinstance(error, (AgentError, PlanError)) else "invalid_mutation"
                )
        plan = (
            self.execution.parse(
                request.plan.model_copy(
                    update={
                        "requests": accepted,
                        "max_requests": len(accepted),
                    }
                ).model_dump()
            )
            if accepted
            else None
        )
        return Adaptation(
            continue_testing=bool(accepted),
            reason=proposal.reason,
            plan=plan,
            mutations=mutations,
            rejected=rejected,
        )

    async def report(self, data: dict, budget: CallBudget):
        return await self.structured("report", data, ReportCommentary, budget)


def apply_mutation(parent: RequestCase, mutation: Mutation, case_id: str) -> RequestCase:
    value = mutation.value
    if isinstance(value, (dict, list)) or any(
        marker in str(value) for marker in ("[REDACTED]", "[PHONE]", "[EMAIL]")
    ):
        raise AgentError("mutation_must_change_one_scalar")
    data = deepcopy(parent.model_dump())
    if mutation.location == "path":
        if mutation.action != "set" or mutation.pointer or not isinstance(value, str):
            raise AgentError("invalid_path_mutation")
        old, new = parent.path.split("/"), value.split("/")
        if len(old) != len(new) or sum(a != b for a, b in zip(old, new, strict=True)) != 1:
            raise AgentError("mutation_must_change_one_scalar")
        data["path"] = value
    else:
        if not is_valid_pointer(mutation.pointer) or not mutation.pointer.startswith("/"):
            raise AgentError("invalid_mutation_pointer")
        parts = pointer_segments(mutation.pointer)
        node = data[mutation.location]
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise AgentError("invalid_mutation_pointer")
            node = node[part]
        key = parts[-1]
        if not isinstance(node, dict) or isinstance(node.get(key), (dict, list)):
            raise AgentError("mutation_must_change_one_scalar")
        if mutation.action == "remove":
            if key not in node:
                raise AgentError("invalid_mutation_pointer")
            del node[key]
        else:
            if mutation.location == "query" and not isinstance(value, str):
                raise AgentError("query_value_must_be_string")
            node[key] = value
    data["id"] = case_id
    data["assertions"] = [assertion.model_dump() for assertion in mutation.assertions]
    return RequestCase.model_validate(data)
