"""AI boundaries are tested independently of the external supplier."""

import json

import pytest

from api_agent.agent.schemas import (
    AdaptRequest,
    DiagnoseRequest,
    GenerateRequest,
)
from api_agent.agent.service import AgentError, AgentService, CallBudget
from api_agent.execution.schemas import ExecutionPlan, RunResult
from api_agent.infrastructure.config import load_settings
from api_agent.llm.client import LLMTimeoutError
from api_agent.llm.schemas import ChatCompletionResponse

CONTRACT = "POST /register: phone 必须是 11 位数字；12 位返回 400，正常返回 200。"


def case(case_id="phone-long", phone="138000000000", expected=400):
    return {
        "id": case_id,
        "method": "POST",
        "path": "/register",
        "body": {"phone": phone},
        "assertions": [{"kind": "status", "expected": expected}],
    }


def plan_payload():
    return {
        "origin": "http://127.0.0.1:9100",
        "allowed_methods": ["POST"],
        "path_templates": ["/register"],
        "requests": [case()],
        "delay_ms": 0,
    }


def generated(requests=None):
    requests = requests or [case()]
    return {
        "requests": requests,
        "notes": ["业务规则来自输入契约"],
        "intents": [
            {
                "case_id": c["id"],
                "category": "boundary",
                "basis": "contract",
                "rationale": "手机号长度边界",
                "source_quote": "12 位返回 400",
            }
            for c in requests
        ],
    }


def diagnosis_payload(evidence_id="r0:phone-long:a0"):
    return {
        "summary": "12 位手机号未按契约拒绝。",
        "hypotheses": [
            {
                "id": "H1",
                "cause": "可能缺少长度校验或异常处理。",
                "evidence_ids": [evidence_id],
                "next_check": "仅改变手机号长度观察差异。",
            }
        ],
        "coverage_gaps": ["尚未验证 10 位与字母"],
    }


def diagnosis_request():
    return DiagnoseRequest(
        contract=CONTRACT,
        plan=ExecutionPlan.model_validate(plan_payload()),
        result=RunResult(
            status="failed",
            reserved_requests=1,
            observed_attempts=1,
            executions=[
                {
                    "id": "phone-long",
                    "status": "failed",
                    "http_status": 500,
                    "assertions": [
                        {
                            "index": 0,
                            "kind": "status",
                            "passed": False,
                            "message": "expected 400 got 500",
                            "pointer": "",
                            "actual": 500,
                            "actual_present": True,
                            "actual_type": "number",
                            "actual_truncated": False,
                        }
                    ],
                }
            ],
        ),
    )


class ScriptedModel:
    """Only the slow, nondeterministic model boundary is replaced."""

    api_key = "sk-private-fixture"
    model = "fixture-model"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.contexts = []

    async def chat_completion(self, request):
        self.contexts.append(request.messages[-1].content)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return ChatCompletionResponse(
            id="fixture",
            model=self.model,
            content=response if isinstance(response, str) else json.dumps(response),
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )


def service(tmp_path, model):
    settings = load_settings(
        env_file=None,
        project_root=tmp_path,
        target_allowed_origins=["http://127.0.0.1:9100"],
    )
    return AgentService(settings, model)


def generate_request(**kwargs):
    return GenerateRequest(
        contract=CONTRACT,
        scope={k: v for k, v in plan_payload().items() if k != "requests"},
        **kwargs,
    )


async def test_generation_returns_plan_accepted_by_real_execution_gate(tmp_path):
    agent = service(tmp_path, ScriptedModel(generated()))
    output = await agent.generate(generate_request(max_cases=2))
    assert agent.execution.validate(output.plan.model_dump())["valid"] is True
    assert output.plan.requests[0].body == {"phone": "138000000000"}
    assert output.intents[0].basis == "contract"


async def test_model_cannot_expand_scope_and_only_one_repair_is_allowed(tmp_path):
    bad = generated([case() | {"path": "/admin"}])
    budget = CallBudget(5)
    agent = service(tmp_path, ScriptedModel(bad, bad, generated()))
    with pytest.raises(AgentError, match="invalid_model_output"):
        await agent.generate(generate_request(), budget)
    assert budget.consumed == 2
    assert len(budget.calls) == 2


async def test_json_repair_is_counted_and_does_not_execute_target(tmp_path):
    budget = CallBudget(2)
    agent = service(tmp_path, ScriptedModel("not JSON", generated()))
    result = await agent.generate(generate_request(), budget)
    assert result.plan.requests[0].id == "phone-long"
    assert budget.consumed == 2
    assert budget.prompt_tokens == 20


async def test_repair_cannot_exceed_budget(tmp_path):
    budget = CallBudget(1)
    with pytest.raises(AgentError, match="model_budget_exhausted"):
        await service(tmp_path, ScriptedModel("[]")).generate(generate_request(), budget)
    assert budget.consumed == 1


async def test_truncated_model_response_retries_with_larger_output_budget(tmp_path):
    limits = []

    class TruncatedModel(ScriptedModel):
        async def chat_completion(self, request):
            limits.append(request.max_tokens)
            if len(limits) == 1:
                return ChatCompletionResponse(
                    id="truncated", model=self.model, content="", finish_reason="length"
                )
            return await super().chat_completion(request)

    budget = CallBudget(2)
    agent = service(tmp_path, TruncatedModel(generated()))
    result = await agent.generate(generate_request(), budget)
    assert result.plan.requests[0].id == "phone-long"
    assert limits[1] > limits[0]
    assert budget.consumed == 2


async def test_inferred_rule_cannot_be_presented_as_contract_quote(tmp_path):
    output = generated()
    output["intents"][0]["source_quote"] = "数据库唯一约束已证实"
    with pytest.raises(AgentError, match="invalid_model_output"):
        await service(tmp_path, ScriptedModel(output, output)).generate(generate_request())


async def test_diagnosis_is_anchored_to_assertion_evidence(tmp_path):
    result = await service(tmp_path, ScriptedModel(diagnosis_payload())).diagnose(
        diagnosis_request()
    )
    assert result.evidence[1].id == "r0:phone-long:a0"
    assert result.evidence[1].expected == 400
    assert result.evidence[1].http_status == 500
    assert result.analysis.hypotheses[0].evidence_ids == ["r0:phone-long:a0"]


async def test_diagnosis_carries_actual_value_and_type_so_type_mismatch_is_visible(tmp_path):
    """实测教训：缺 actual/actual_type 时，模型分不清“值不符”与“类型不符”。

    期望数字 400、目标返回字符串 "400" 会被判失败，但这属于测试期望的类型推断错误，
    不是业务缺陷。证据必须带上实际值与类型，模型和报告才有依据区分。
    """
    result = await service(tmp_path, ScriptedModel(diagnosis_payload())).diagnose(
        diagnosis_request()
    )
    failing = next(
        item
        for item in result.evidence
        if item.assertion_index is not None and item.passed is False
    )
    assert failing.actual_present is True
    assert failing.actual_type is not None


async def test_diagnosis_rejects_invented_evidence(tmp_path):
    bad = diagnosis_payload("not-a-real-case")
    with pytest.raises(AgentError, match="invalid_model_output"):
        await service(tmp_path, ScriptedModel(bad, bad)).diagnose(diagnosis_request())


async def test_result_plan_mismatch_is_rejected_before_model_call(tmp_path):
    request = diagnosis_request()
    request.result.executions[0].id = "other"
    with pytest.raises(AgentError, match="result_plan_mismatch"):
        await service(tmp_path, ScriptedModel()).diagnose(request)


def mutation(value="1380000000", **kwargs):
    return {
        "parent_id": "phone-long",
        "hypothesis_id": "H1",
        "location": "body",
        "pointer": "/phone",
        "action": "set",
        "value": value,
        "assertions": [{"kind": "status", "expected": 400}],
        "reason": "单独改变长度以检验 H1",
        **kwargs,
    }


async def adaptation(tmp_path, mutations, *, remaining=2, allow=False):
    agent = service(
        tmp_path,
        ScriptedModel(
            diagnosis_payload(),
            {
                "continue_testing": True,
                "reason": "验证长度维度",
                "mutations": mutations,
            },
        ),
    )
    diagnosis = await agent.diagnose(diagnosis_request())
    request = AdaptRequest(
        contract=CONTRACT,
        plan=ExecutionPlan.model_validate(plan_payload()),
        diagnosis=diagnosis,
        remaining_requests=remaining,
        allow_mutating_followups=allow,
    )
    return agent, request, await agent.adapt(request)


async def test_adaptation_applies_single_leaf_and_preserves_envelope(tmp_path):
    _, _, output = await adaptation(tmp_path, [mutation()], allow=True)
    assert output.plan.origin == "http://127.0.0.1:9100"
    assert output.plan.requests[0].body == {"phone": "1380000000"}
    assert output.plan.requests[0].method == "POST"
    assert output.continue_testing is True


async def test_mutating_followups_require_explicit_permission(tmp_path):
    _, _, output = await adaptation(tmp_path, [mutation()])
    assert output.plan is None
    assert "mutating_followup_not_allowed" in output.rejected


async def test_adaptation_rejects_duplicate_and_unknown_hypothesis(tmp_path):
    _, _, output = await adaptation(
        tmp_path,
        [
            mutation(value="138000000000"),
            mutation(hypothesis_id="invented"),
            mutation(),
            mutation(),
        ],
        allow=True,
    )
    assert len(output.plan.requests) == 1
    assert "duplicate_request" in output.rejected
    assert "unknown_hypothesis" in output.rejected


async def test_adaptation_respects_remaining_request_budget(tmp_path):
    _, _, output = await adaptation(
        tmp_path, [mutation(), mutation(value="letters")], remaining=1, allow=True
    )
    assert len(output.plan.requests) == 1
    assert "request_budget_exhausted" in output.rejected


async def test_model_timeout_is_typed_and_charged(tmp_path):
    budget = CallBudget(2)
    with pytest.raises(AgentError, match="model_timeout"):
        await service(tmp_path, ScriptedModel(LLMTimeoutError("secret upstream text"))).generate(
            generate_request(), budget
        )
    assert budget.consumed == 1
    assert budget.calls[0].error_kind == "model_timeout"


async def test_context_redacts_secrets_without_destroying_schema(tmp_path):
    model = ScriptedModel(generated())
    agent = service(tmp_path, model)
    request = generate_request()
    request.contract = {
        "description": CONTRACT + " Bearer private-token sk-private-fixture",
        "properties": {"password": {"type": "string", "minLength": 8}},
        "example": {"password": "real-password", "phone": "13812345678"},
    }
    await agent.generate(request)
    text = model.contexts[0]
    assert "private-token" not in text
    assert "sk-private-fixture" not in text
    assert "real-password" not in text
    assert '"minLength": 8' in text


async def test_diagnosis_scrubs_sensitive_request_values_from_assertion_messages(tmp_path):
    request = diagnosis_request()
    request.plan.requests[0].body["password"] = "real-input-password"
    request.result.executions[0].assertions[0].message = "actual real-input-password"
    model = ScriptedModel(diagnosis_payload())
    result = await service(tmp_path, model).diagnose(request)
    assert "real-input-password" not in model.contexts[0]
    assert "real-input-password" not in result.model_dump_json()


async def test_model_cannot_echo_live_credential_in_generated_notes(tmp_path):
    bad = generated()
    bad["notes"] = ["sk-private-fixture"]
    with pytest.raises(AgentError, match="invalid_model_output"):
        await service(tmp_path, ScriptedModel(bad, bad)).generate(generate_request())


async def test_sensitive_schema_examples_are_scrubbed_but_constraints_survive(tmp_path):
    request = generate_request()
    request.contract = {
        "description": CONTRACT,
        "properties": {
            "password": {
                "type": "string",
                "minLength": 8,
                "default": "prod-default-secret",
                "example": "prod-example-secret",
                "enum": ["prod-enum-secret"],
            }
        },
    }
    model = ScriptedModel(generated())
    await service(tmp_path, model).generate(request)
    assert "prod-default-secret" not in model.contexts[0]
    assert "prod-example-secret" not in model.contexts[0]
    assert "prod-enum-secret" not in model.contexts[0]
    assert '"minLength": 8' in model.contexts[0]


@pytest.mark.parametrize("composition", ["allOf", "anyOf", "oneOf"])
async def test_composed_sensitive_schema_propagates_redaction(tmp_path, composition):
    request = generate_request()
    request.contract = {
        "description": CONTRACT,
        "properties": {
            "password": {
                composition: [
                    {
                        "type": "string",
                        "minLength": 8,
                        "default": "prod-composed-secret",
                        "example": "prod-composed-example",
                    }
                ]
            }
        },
    }
    model = ScriptedModel(generated())
    await service(tmp_path, model).generate(request)
    assert "prod-composed-secret" not in model.contexts[0]
    assert "prod-composed-example" not in model.contexts[0]
    assert '"minLength": 8' in model.contexts[0]
