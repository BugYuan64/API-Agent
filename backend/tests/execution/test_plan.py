import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from api_agent.execution.collection import compile_collection
from api_agent.execution.schemas import ExecutionPlan, validate_scope


def plan_data(**changes):
    value = {
        "origin": "http://127.0.0.1:8080",
        "allowed_methods": ["GET", "POST"],
        "path_templates": ["/api/user/login", "/api/customer/{id}"],
        "requests": [
            {
                "id": "invalid-login",
                "method": "POST",
                "path": "/api/user/login",
                "body": {},
                "assertions": [
                    {"kind": "status", "expected": 200},
                    {"kind": "json_equals", "pointer": "/code", "expected": "400"},
                ],
            }
        ],
    }
    value.update(changes)
    return value


def test_compile_fixed_scripts_and_json_data_without_hidden_requests():
    data = plan_data()
    text = "'); pm.sendRequest('https://example.com'); //"
    data["requests"][0]["body"] = {"email": text}
    plan = ExecutionPlan.model_validate(data)
    validate_scope(plan, [plan.origin], [])
    collection = compile_collection(plan, {})
    item = collection["item"][0]
    assert json.loads(item["request"]["body"]["raw"]) == {"email": text}
    assert item["request"]["url"] == plan.origin + "/api/user/login"
    script = "\n".join(item["event"][0]["script"]["exec"])
    assert "pm.sendRequest" not in script
    assert "a0_status" in script and "a1_json_equals" in script


@pytest.mark.parametrize(
    "path", ["https://example.com", "//evil", "/../a", "/a%2fb", "/a?x=1", "/a\\b"]
)
def test_unsafe_paths_are_rejected(path):
    data = plan_data()
    data["requests"][0]["path"] = path
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(data)


def test_scope_and_local_secret_permissions_are_independent_of_plan():
    plan = ExecutionPlan.model_validate(plan_data())
    # 白名单为空时默认放行合法 origin；配置白名单时严格限制
    validate_scope(plan, [], [])
    with pytest.raises(ValueError, match="origin_not_allowed"):
        validate_scope(plan, ["http://127.0.0.1:9999"], [])
    data = plan_data(auth_ref={"kind": "header", "header_name": "token", "env_var": "OTHER_KEY"})
    with pytest.raises(ValueError, match="auth_reference_not_allowed"):
        validate_scope(ExecutionPlan.model_validate(data), [plan.origin], ["CRM_TEST_TOKEN"])


@pytest.mark.parametrize(
    ("raw_origin", "normalized"),
    [
        ("https://API.Example.com", "https://api.example.com"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://192.168.1.10:8080/", "http://192.168.1.10:8080"),
        ("https://8.8.8.8", "https://8.8.8.8"),
    ],
)
def test_execution_plan_accepts_valid_domains_and_ips(raw_origin, normalized):
    plan = ExecutionPlan.model_validate(plan_data(origin=raw_origin))
    assert plan.origin == normalized


@pytest.mark.parametrize(
    "change",
    [
        {"origin": "ftp://example.com"},
        {"origin": "https://user:pass@example.com"},
        {"origin": "http://127.0.0.1:8080/path"},
        {"origin": "http://bad host:8080"},
        {"max_requests": 0},
        {"script": "pm.sendRequest('x')"},
    ],
)
def test_reject_invalid_origins_and_arbitrary_fields(change):
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(plan_data(**change))


def test_unlisted_method_path_duplicate_id_headers_and_empty_assertions_are_rejected():
    for change in (
        {"method": "DELETE"},
        {"path": "/not-listed"},
        {"headers": {"Host": "evil"}},
        {"headers": {"Authorization": "secret"}},
        {"assertions": []},
        {"body": {"x": "{{secret}}"}},
    ):
        data = plan_data()
        data["requests"][0].update(change)
        with pytest.raises(ValidationError):
            ExecutionPlan.model_validate(data)
    data = plan_data()
    data["requests"] *= 2
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(data)


def test_pointer_expected_value_is_encoded_as_data_and_query_is_escaped():
    data = plan_data()
    data["requests"][0].update(query={"keyword": "a&b#c"})
    plan = ExecutionPlan.model_validate(data)
    item = compile_collection(plan, {})["item"][0]
    assert item["request"]["url"].endswith("?keyword=a%26b%23c")


def test_assertion_types_and_size_are_bounded():
    for assertion in (
        {"kind": "status", "expected": True},
        {"kind": "status", "expected": 999},
        {"kind": "json_exists", "pointer": "not-a-pointer"},
        {"kind": "response_time", "expected": -1},
    ):
        data = plan_data()
        data["requests"][0]["assertions"] = [assertion]
        with pytest.raises(ValidationError):
            ExecutionPlan.model_validate(data)


def test_long_invalid_path_is_rejected_without_excessive_backtracking():
    program = """
from pydantic import ValidationError
from api_agent.execution.schemas import RequestCase
try:
    RequestCase(id='case', method='GET', path='/' + 'a' * 250 + '!',
                assertions=[{'kind': 'status', 'expected': 200}])
except ValidationError:
    pass
else:
    raise AssertionError('unsafe path accepted')
"""
    subprocess.run([sys.executable, "-c", program], timeout=3, check=True)


def test_nested_json_objects_are_data_not_unbound_variables():
    data = plan_data()
    data["requests"][0]["body"] = {"profile": {"name": "fixture"}}
    data["requests"][0]["assertions"][1]["expected"] = {"nested": {"ok": True}}
    plan = ExecutionPlan.model_validate(data)
    collection = compile_collection(plan, {})
    assert json.loads(collection["item"][0]["request"]["body"]["raw"]) == {
        "profile": {"name": "fixture"},
    }


@pytest.mark.parametrize("expected", [9007199254740993, {"id": -9007199254740993}])
def test_equality_rejects_integers_not_exactly_representable_by_postman(expected):
    data = plan_data()
    data["requests"][0]["assertions"][1]["expected"] = expected
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(data)


def test_compiled_headers_are_unique_case_insensitively():
    data = plan_data()
    data["requests"][0]["headers"] = {
        "accept": "application/json",
        "content-type": "application/json",
    }
    collection = compile_collection(ExecutionPlan.model_validate(data), {})
    names = [header["key"].lower() for header in collection["item"][0]["request"]["header"]]
    assert len(names) == len(set(names))
