import json
from pathlib import Path

from api_agent.execution.schemas import ExecutionPlan


def test_crm_examples_match_the_executable_schema():
    directory = Path(__file__).resolve().parents[3] / "examples/crm"
    plans = {
        path.stem: ExecutionPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in directory.glob("*.json")
    }
    assert set(plans) == {
        "smoke",
        "authenticated",
        "rag",
        "docker-smoke",
        "docker-authenticated",
        "docker-register-validation",
        "docker-rag",
        "docker-stateful-crud",
    }
    assert len(plans["smoke"].requests) == 8
    assert plans["authenticated"].auth_ref.header_name == "token"
    assert all(case.body == {"question": ""} for case in plans["rag"].requests[1:])
    # Docker 样例指向统一入口 8088 与 RAG 8002，复用自定义 token 头与受控变量链约定。
    assert plans["docker-smoke"].origin == "http://127.0.0.1:8088"
    assert plans["docker-authenticated"].origin == "http://127.0.0.1:8088"
    assert plans["docker-authenticated"].auth_ref.header_name == "token"
    assert all(case.method == "GET" for case in plans["docker-authenticated"].requests)
    assert len(plans["docker-register-validation"].requests) == 6
    assert plans["docker-rag"].origin == "http://127.0.0.1:8002"
    assert len(plans["docker-stateful-crud"].requests) == 11
    assert plans["docker-stateful-crud"].requests[0].extract[0].name == "crm_token"
    assert plans["docker-stateful-crud"].requests[0].extract[0].sensitive is True

    self_dir = Path(__file__).resolve().parents[3] / "examples/self"
    self_plan = ExecutionPlan.model_validate(
        json.loads((self_dir / "management-smoke.json").read_text(encoding="utf-8"))
    )
    assert self_plan.origin == "http://127.0.0.1:8010"
    assert len(self_plan.requests) == 13
