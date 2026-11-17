"""接口文档导出回归，验证未知契约、嵌套字段、部分结果和不可信文字的保留方式。"""

import pytest
from pydantic import ValidationError

from api_agent.projects.document_export import render_markdown, render_openapi
from api_agent.projects.document_schema import DocumentEndpoint


def record(*endpoints, **overrides):
    """构造独立导出记录，避免为纯序列化测试启动任务或模型。"""
    return {
        "project_name": "Example",
        "source_id": "snapshot",
        "status": "partial",
        "processed_files": 3,
        "total_files": 5,
        "processed_batches": 1,
        "total_batches": 2,
        "scan_complete": True,
        "warnings": ["动态路由尚未确认"],
        "endpoints": list(endpoints),
        **overrides,
    }


def endpoint(**overrides):
    """生成最小有据接口，允许各测试只覆盖相关字段。"""
    return {"method": "GET", "path": "/items/{id}", "source_files": ["routes.py"], **overrides}


def test_unknown_contract_does_not_invent_status_type_or_security():
    output = render_openapi(record(endpoint()))
    operation = output["paths"]["/items/{id}"]["get"]
    assert list(operation["responses"]) == ["default"]
    assert operation["parameters"][0]["schema"] == {}
    assert operation["parameters"][0]["required"] is True
    assert "security" not in operation
    assert "requestBody" not in operation
    assert output["x-generation"]["runtime_routes_verified"] is False
    assert output["x-generation"]["status"] == "partial"


def test_nested_contract_and_all_protocols_are_preserved():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"name": {"type": "string"}}},
            }
        },
    }
    source = record(
        endpoint(
            method="POST",
            request_schema=schema,
            request_content_type="application/json",
            responses=[
                {
                    "status": "201",
                    "description": "created",
                    "body_schema": schema,
                    "content_type": "application/json",
                }
            ],
        ),
        endpoint(method="WS", path="/events", summary="WebSocket", source_files=["socket.py"]),
    )
    output = render_openapi(source)
    operation = output["paths"]["/items/{id}"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == schema
    assert operation["responses"]["201"]["content"]["application/json"]["schema"] == schema
    assert output["x-websocket-operations"][0]["path"] == "/events"
    markdown = render_markdown(source)
    assert "WEBSOCKET /events" in markdown
    assert '"name"' in markdown
    assert "3 / 5" in markdown
    assert "socket.py" in markdown


def test_markdown_escapes_injected_markup_and_fence_termination():
    source = record(
        endpoint(
            summary='<img src=x onerror="bad()"> | [click](javascript:bad)',
            description="## fake\n<script>bad</script>",
            request_schema={"description": "```\n<script>bad</script>"},
        ),
        project_name="<script>bad</script>",
    )
    output = render_markdown(source)
    assert "# &lt;script&gt;bad&lt;/script&gt; 接口文档" in output
    assert "\\| \\[click\\]\\(javascript:bad\\)" in output
    assert "````json" in output
    assert "\\#\\# fake &lt;script&gt;bad&lt;/script&gt;" in output


def test_duplicate_operations_remain_available_as_variants():
    output = render_openapi(record(endpoint(summary="first"), endpoint(summary="second")))
    operation = output["paths"]["/items/{id}"]["get"]
    assert operation["summary"] == "first"
    assert operation["x-definition-variants"][0]["summary"] == "second"


def test_embedded_schema_references_resolve_in_export_and_unknown_refs_are_marked():
    schema = {
        "$defs": {"Item": {"type": "string"}},
        "type": "object",
        "properties": {
            "item": {"$ref": "#/$defs/Item"},
            "unknown": {"$ref": "#/components/schemas/Missing"},
        },
    }
    source = record(endpoint(request_schema=schema, request_content_type="application/json"))
    output = render_openapi(source)
    body = output["paths"]["/items/{id}"]["get"]["requestBody"]
    properties = body["content"]["application/json"]["schema"]["properties"]
    reference = properties["item"]["$ref"]
    target = output
    for part in reference[2:].split("/"):
        target = target[part.replace("~1", "/").replace("~0", "~")]
    assert target == {"type": "string"}
    assert "$ref" not in properties["unknown"]
    assert properties["unknown"]["x-unresolved-ref"] == "#/components/schemas/Missing"
    assert source["endpoints"][0]["request_schema"]["properties"]["item"]["$ref"] == "#/$defs/Item"
    assert "$ref" in source["endpoints"][0]["request_schema"]["properties"]["unknown"]


@pytest.mark.parametrize(
    "change",
    [
        {"source_files": ["../secret.py"]},
        {"source_files": ["C:/private/file.py"]},
        {"path": "https://example.test/items"},
        {"request_schema": {"$ref": "https://example.test/schema"}},
        {"responses": [{"status": "success"}]},
    ],
)
def test_model_contract_rejects_invalid_references(change):
    with pytest.raises(ValidationError):
        DocumentEndpoint.model_validate(endpoint(**change))
