"""Secrets are learned consistently across contracts and pointer-based assertions."""

import json

import pytest

from api_agent.infrastructure.logging import REDACTED, RedactingJsonFormatter
from api_agent.infrastructure.privacy import Context


def test_contract_json_string_learns_sensitive_schema_values_and_unlabelled_echoes():
    context = Context()
    contract = {
        "type": "object",
        "properties": {
            "password": {
                "type": "string",
                "minLength": 8,
                "example": "fixture-example-password",
                "default": "fixture-default-password",
                "enum": ["fixture-enum-password"],
            },
            "confirmPassword": {
                "allOf": [{"type": "string", "example": "fixture-confirm-password"}]
            },
        },
        "description": "echo fixture-example-password and fixture-confirm-password",
    }
    data = {"contract": json.dumps(contract), "notes": "echo fixture-default-password"}
    context.learn(data)
    cleaned = context.clean(data)
    serialized = json.dumps(cleaned)
    for value in (
        "fixture-example-password",
        "fixture-default-password",
        "fixture-enum-password",
        "fixture-confirm-password",
    ):
        assert value not in serialized
        assert value not in context.text(f"later echo {value}")
    parsed = json.loads(cleaned["contract"])
    assert parsed["properties"]["password"]["type"] == "string"
    assert parsed["properties"]["password"]["minLength"] == 8
    assert parsed["properties"]["confirmPassword"]["allOf"][0]["type"] == "string"


@pytest.mark.parametrize(
    "pointer",
    ["/data/token", "/data/access_token", "/password", "/data/confirmPassword", "/password/nested"],
)
def test_assertion_pointer_redacts_expected_actual_and_learns_echoes(pointer):
    context = Context()
    data = {
        "plan": {
            "assertions": [
                {
                    "kind": "json_equals",
                    "pointer": pointer,
                    "expected": "fixture-expected-secret",
                    "actual": {"value": "fixture-actual-secret"},
                }
            ]
        },
        "notes": "echo fixture-expected-secret and fixture-actual-secret",
    }
    context.learn(data)
    cleaned = context.clean(data)
    assertion = cleaned["plan"]["assertions"][0]
    assert assertion["pointer"] == pointer
    assert assertion["expected"] == REDACTED
    assert assertion["actual"] == REDACTED
    assert "fixture-expected-secret" not in json.dumps(cleaned)
    assert "fixture-actual-secret" not in json.dumps(cleaned)


def test_pointer_redaction_does_not_depend_on_prior_learning_or_value_length():
    context = Context()
    assert context.assertion_value("/data/token", "x") == REDACTED
    assert context.assertion_value("/data/password", {"v": 123}) == REDACTED
    cleaned = context.clean({"pointer": "/data/token", "expected": "x", "actual": 123})
    assert cleaned == {"pointer": "/data/token", "expected": REDACTED, "actual": REDACTED}
    assert context.assertion_value("/data/name", "fixture-ordinary") == "fixture-ordinary"
    assert context.clean({"pointer": "/data/count", "expected": 8})["expected"] == 8


def test_confirm_password_is_sensitive_in_structured_and_free_text_logging():
    redactor = RedactingJsonFormatter(secrets=["fixture-known-secret"])
    result = redactor.redact(
        {
            "confirmPassword": "fixture-confirm-value",
            "notes": "confirmPassword=fixture-confirm-value",
            "echo": "fixture-known-secret",
        }
    )
    assert result["confirmPassword"] == REDACTED
    assert "fixture-confirm-value" not in json.dumps(result)
    assert result["echo"] == REDACTED


@pytest.mark.parametrize("as_string", [False, True])
def test_direct_contract_normalizes_and_learns_before_cleaning(as_string):
    contract = {
        "properties": {"password": {"type": "string", "example": "fixture-secret"}},
        "description": "echo fixture-secret",
    }
    context = Context()
    value = json.dumps(contract) if as_string else contract
    cleaned = context.contract(value)
    assert isinstance(cleaned, type(value))
    assert "fixture-secret" not in json.dumps(cleaned)
    assert "fixture-secret" not in context.text("future fixture-secret")


def test_escaped_pointer_sensitive_parent_learns_mutation_value_and_message():
    context = Context()
    data = {
        "pointer": "/data~1nested/access_token/nested~0name",
        "value": "fixture-mutation-secret",
        "message": "echo fixture-mutation-secret",
    }
    context.learn(data)
    cleaned = context.clean(data)
    assert cleaned["value"] == REDACTED
    assert "fixture-mutation-secret" not in cleaned["message"]


@pytest.mark.parametrize(
    "mode,value",
    [
        ("json", '{"password":"fixture-body-secret","echo":"fixture-body-secret"}'),
        ("form", "password=fixture%2Dbody%2Dsecret&echo=fixture%2Dbody%2Dsecret&count=8"),
        ("text", "password=fixture-body-secret"),
    ],
)
def test_body_decodes_sensitive_values_before_cleaning(mode, value):
    context = Context()
    cleaned = context.body(value, mode=mode)
    assert "fixture-body-secret" not in cleaned
    assert "fixture%2Dbody%2Dsecret" not in cleaned
    if mode == "form":
        assert "count=8" in cleaned
    assert context.body(None, mode=mode) is None


def test_text_body_learns_assignment_value_for_later_echoes():
    context = Context()
    cleaned = context.body("password=fixture-body-secret; echo fixture-body-secret", mode="text")
    assert "fixture-body-secret" not in cleaned
    assert "fixture-body-secret" not in context.text("future fixture-body-secret")


def test_secret_matching_schema_keyword_does_not_destroy_structural_constraints():
    context = Context()
    result = context.contract(
        {
            "type": "object",
            "required": ["password"],
            "properties": {"password": {"type": "string", "minLength": 8, "example": "string"}},
        }
    )
    assert result["type"] == "object"
    assert result["required"] == ["password"]
    assert result["properties"]["password"]["type"] == "string"
    assert result["properties"]["password"]["minLength"] == 8
    assert result["properties"]["password"]["example"] == REDACTED


def test_sensitive_pointer_is_metadata_even_when_expected_equals_property_name():
    context = Context()
    result = context.clean({"pointer": "/data/token", "expected": "token", "actual": "token"})
    assert result == {"pointer": "/data/token", "expected": REDACTED, "actual": REDACTED}


def test_dumps_learns_all_fields_before_scrubbing_earlier_unlabelled_echo():
    context = Context()
    serialized = context.dumps({"echo": "fixture-password", "password": "fixture-password"})
    assert "fixture-password" not in serialized


def test_evidence_ids_are_never_mistaken_for_credentials():
    """结构化证据 ID 不能被赋值式凭据正则误伤。

    回归来源：``r0:login-wrong-password:a0=500`` 里的 ``password`` 后面跟
    ``:a0=500``，旧正则会把它当成 “password=值”，整段替换为 ``[REDACTED]``，
    使模型引用的正确证据 ID 永远匹配不上，诊断为 invalid_model_output。
    """
    context = Context()

    # 证据 ID 原样保留（含 case 名里出现 password/token 的情况）
    for text in (
        "r0:login-wrong-password:a0=500",
        "r0:login-wrong-password:a0",
        "r0:login-invalid-token:a1",
    ):
        assert context.text(text) == text

    # 真正的赋值式凭据仍然脱敏，且保留分隔符与引号
    assert context.text("password=secret123") == "password=[REDACTED]"
    assert context.text("token: abcdef") == "token: [REDACTED]"
    assert context.text('"password": "hunter2"') == '"password": [REDACTED]'
    assert context.text("token=xyz&next=1") == "token=[REDACTED]&next=1"
