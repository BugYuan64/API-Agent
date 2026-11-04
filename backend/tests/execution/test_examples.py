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
    }
    assert len(plans["smoke"].requests) == 8
    assert plans["authenticated"].auth_ref.header_name == "token"
    assert all(case.body == {"question": ""} for case in plans["rag"].requests[1:])
    # Docker 样例指向容器统一入口（Nginx 8088），复用同一自定义 token 头约定。
    assert plans["docker-smoke"].origin == "http://127.0.0.1:8088"
    assert plans["docker-authenticated"].origin == "http://127.0.0.1:8088"
    assert plans["docker-authenticated"].auth_ref.header_name == "token"
    assert all(case.method == "GET" for case in plans["docker-authenticated"].requests)
