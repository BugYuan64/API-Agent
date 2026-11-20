"""项目源码导入与接口文档正式入口回归，使用脚本模型核对覆盖、版本和失败收尾。"""

import asyncio
import json

import httpx
import pytest

from api_agent.agent.routes import agent_for
from api_agent.agent.service import AgentService
from api_agent.llm.errors import LLMConnectionError
from api_agent.llm.schemas import ChatCompletionResponse
from api_agent.projects.document_routes import legacy_writer
from api_agent.projects.document_schema import DocumentEndpoint
from api_agent.projects.documents import PENDING, merge_endpoints, source_batches
from api_agent.tasks import project_document
from api_agent.tasks import routes as task_routes


class SourceModel:
    """从收到的批次清单返回可预测结果，可注入引用错误、传输故障和等待取消。"""

    model = "document-fixture"

    def __init__(self, *, invalid=False, fail_after=None, block=False, empty=False):
        """保留调用输入以核对每个文件分块，同时提供可控的后台任务同步点。"""
        self.calls = []
        self.invalid = invalid
        self.fail_after = fail_after
        self.block = block
        self.empty = empty
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_completion(self, request):
        """读取正式 AgentService 消息，在修复轮仍解析首个 JSON 对象。"""
        payload, _ = json.JSONDecoder().raw_decode(request.messages[1].content)
        self.calls.append(payload)
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise LLMConnectionError("fixture transport failure")
        endpoints = []
        for route in payload["known_routes"]:
            item = {
                "method": route["method"],
                "path": route["path"],
                "source_files": ["missing.py" if self.invalid else route["file"]],
            }
            if not self.empty:
                item.update(
                    summary="源码声明的接口",
                    description="已结合本批源码核对声明。",
                    responses=[{"status": "200", "description": "源码声明的响应"}],
                )
            endpoints.append(item)
        content = {
            "reviewed_files": list(dict.fromkeys(item["path"] for item in payload["source_files"])),
            "endpoints": endpoints,
            "warnings": [],
        }
        return ChatCompletionResponse(
            id="fixture", model=self.model, content=json.dumps(content), finish_reason="stop"
        )


def install_model(app, settings, model):
    """替换已配置模型的依赖，继续使用真实 AgentService 校验与预算流程。"""

    async def dependency():
        """每次创建请求得到独立 Agent 上下文，模型脚本跨请求保留计数。"""
        return AgentService(settings, model)

    app.dependency_overrides[agent_for] = dependency


def python_route(path="/items"):
    """构造扫描器可静态识别的 FastAPI 源文件，不执行导入的项目。"""
    return (
        "from fastapi import FastAPI\napp = FastAPI()\n"
        f"@app.get('{path}')\ndef items():\n    return {{'ok': True}}\n"
    )


@pytest.fixture
async def archive_client(app, client):
    """Mount the retired writer only to exercise old data compatibility in these tests."""
    app.include_router(legacy_writer)
    return client


async def import_project(client, directory, name="Document project"):
    """Create an archived project fixture with the legacy writer."""
    response = await client.post(
        "/api/projects/import", json={"name": name, "directory": str(directory)}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def start_document(client, project_id, source_id):
    """从明确的源码版本启动生成，并检查后台任务 Location 契约。"""
    response = await client.post(
        f"/api/projects/{project_id}/documents", json={"source_id": source_id}
    )
    assert response.status_code == 202, response.text
    data = response.json()
    assert response.headers["location"].endswith(data["id"])
    return data


async def finished_document(client, app, project_id, document_id):
    """等待已受理的后台协程结束后从正式详情接口读取持久记录。"""
    active = list(app.state.documents.active.values())
    async with asyncio.timeout(15):
        await asyncio.gather(*active)
    response = await client.get(f"/api/projects/{project_id}/documents/{document_id}")
    assert response.status_code == 200
    return response.json()


async def test_smart_test_writes_openapi_without_a_source_snapshot(
    app, settings, tmp_path, monkeypatch
):
    """Use an explicit desktop selection and leave only its OpenAPI export on disk."""
    project = tmp_path / "sample project"
    project.mkdir()
    (project / "app.py").write_text(python_route("/health"), encoding="utf-8")
    model = SourceModel()

    async def configured_agent(request, session):
        return AgentService(settings, model)

    monkeypatch.setattr(task_routes, "agent_for", configured_agent)
    monkeypatch.setattr(project_document, "choose_directory", lambda: project)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        legacy = await client.post(
            "/api/projects/import", json={"name": "old", "directory": str(project)}
        )
        assert legacy.status_code in {404, 405}
        forbidden = await client.post(
            "/api/tasks/generate-document", headers={"Origin": "https://elsewhere.invalid"}
        )
        assert forbidden.status_code == 403
        response = await client.post(
            "/api/tasks/generate-document", headers={"Origin": "http://localhost"}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["filename"].startswith("openapi") and result["filename"].endswith(".json")
        output = project / "API" / result["filename"]
        assert output.read_text(encoding="utf-8") == result["text"]
        assert "/health" in json.loads(result["text"])["paths"]
        assert not list(app.state.documents.store.root.rglob("*.json"))
        assert (await client.get("/api/projects")).json()["items"] == []
        assert len(model.calls) == 1


async def test_import_all_batches_tail_file_and_both_exports(
    archive_client, app, settings, tmp_path
):
    """完整源码每个字符都送入模型，最后一个文件的接口进入预览和两种下载。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    contents = {
        "first.py": python_route("/first"),
        "large.py": "# source context\n" * 1800 + python_route("/large"),
        "zz_last.py": python_route("/last"),
    }
    for name, content in contents.items():
        (directory / name).write_text(content, encoding="utf-8", newline="\n")
    (directory / ".env").write_text("API_KEY=fixture-private-token", encoding="utf-8")
    model = SourceModel()
    install_model(app, settings, model)
    imported = await import_project(client, directory)
    project_id = imported["project"]["id"]
    source = imported["source"]
    assert source["scanned_files"] == 3
    assert "files" not in source
    started = await start_document(client, project_id, source["id"])
    record = await finished_document(client, app, project_id, started["id"])
    assert record["status"] == "completed"
    assert record["processed_files"] == record["total_files"] == 3
    assert record["processed_batches"] == record["total_batches"] == len(model.calls)
    assert record["total_batches"] > 1
    actual = {name: "" for name in contents}
    for call in model.calls:
        for item in call["source_files"]:
            actual[item["path"]] += item["content"]
    assert actual == contents
    assert {item["path"] for item in record["endpoints"]} == {"/first", "/large", "/last"}
    assert "fixture-private-token" not in json.dumps(model.calls)
    prefix = f"/api/projects/{project_id}/documents/{record['id']}"
    markdown = await client.get(f"{prefix}/export?format=markdown")
    openapi = await client.get(f"{prefix}/export?format=openapi")
    assert markdown.status_code == openapi.status_code == 200
    assert "attachment" in markdown.headers["content-disposition"]
    assert "/last" in markdown.text
    assert set(openapi.json()["paths"]) == {"/first", "/large", "/last"}
    assert openapi.json()["x-generation"]["processed_files"] == 3
    history = await client.get(f"/api/projects/{project_id}/documents")
    assert history.json()["items"][0]["id"] == record["id"]


async def test_reimport_freezes_old_version_and_cross_project_access_is_rejected(
    archive_client, app, settings, tmp_path
):
    """重导入生成新版本，旧版本仍可生成且其他项目不可使用该源码或文档 ID。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    source_file = directory / "routes.py"
    source_file.write_text(python_route("/v1"), encoding="utf-8")
    imported = await import_project(client, directory)
    project_id = imported["project"]["id"]
    first_id = imported["source"]["id"]
    source_file.write_text(python_route("/v2"), encoding="utf-8")
    updated = await client.post(
        f"/api/projects/{project_id}/source", json={"directory": str(directory)}
    )
    assert updated.status_code == 201
    assert updated.json()["id"] != first_id
    assert updated.json()["sha256"] != imported["source"]["sha256"]
    latest = await client.get(f"/api/projects/{project_id}/source")
    assert latest.json()["id"] == updated.json()["id"]
    install_model(app, settings, SourceModel())
    started = await start_document(client, project_id, first_id)
    record = await finished_document(client, app, project_id, started["id"])
    assert {item["path"] for item in record["endpoints"]} == {"/v1"}
    other = await client.post("/api/projects", json={"name": "Other project"})
    other_id = other.json()["id"]
    rejected = await client.post(
        f"/api/projects/{other_id}/documents", json={"source_id": first_id}
    )
    assert rejected.status_code == 404
    for suffix in ("", "/export?format=markdown"):
        result = await client.get(f"/api/projects/{other_id}/documents/{record['id']}{suffix}")
        assert result.status_code == 404


async def test_model_context_preserves_source_types_and_redacts_literal_credentials(
    archive_client, app, settings, tmp_path
):
    """源码脱敏保留 password 类型注解和重复 str 类型，示例密钥及回显均移除。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    content = (
        "class Login:\n"
        "    password: str\n"
        "    email: str\n"
        "    secret: str = 'fixture-sensitive-literal'\n"
        "password = 'str'\n"
        "reused_type = str\n"
        "echo = 'fixture-sensitive-literal'\n"
        "def parse(value: str) -> str:\n    return value\n" + python_route()
    )
    (directory / "routes.py").write_text(content, encoding="utf-8", newline="\n")
    model = SourceModel()
    install_model(app, settings, model)
    imported = await import_project(client, directory)
    project_id = imported["project"]["id"]
    source_id = imported["source"]["id"]
    started = await start_document(client, project_id, source_id)
    record = await finished_document(client, app, project_id, started["id"])
    assert record["status"] == "completed"
    supplied = "".join(item["content"] for call in model.calls for item in call["source_files"])
    for declaration in (
        "password: str",
        "email: str",
        "secret: str =",
        "reused_type = str",
        "def parse(value: str) -> str:",
    ):
        assert declaration in supplied
    assert "fixture-sensitive-literal" not in json.dumps(model.calls)
    assert "password = 'str'" not in supplied
    assert "[REDACTED]" in supplied
    persisted = app.state.documents.store.read(project_id, "sources", source_id)
    assert "fixture-sensitive-literal" not in json.dumps(persisted)
    assert "password: str" in persisted["files"][0]["content"]


async def test_invalid_source_reference_is_repaired_once_then_saved_failed(
    archive_client, app, settings, tmp_path
):
    """模型引用未提供文件时触发一次修复，持续错误保留静态清单并标记失败。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "routes.py").write_text(python_route(), encoding="utf-8")
    model = SourceModel(invalid=True)
    install_model(app, settings, model)
    imported = await import_project(client, directory)
    project_id = imported["project"]["id"]
    started = await start_document(client, project_id, imported["source"]["id"])
    record = await finished_document(client, app, project_id, started["id"])
    assert record["status"] == "failed"
    assert record["processed_files"] == record["processed_batches"] == 0
    assert len(model.calls) == record["model_calls"] == 2
    assert "invalid_model_output" in record["error"]
    assert record["endpoints"][0]["source_files"] == ["routes.py"]
    assert PENDING in record["endpoints"][0]["uncertainties"]
    assert "missing.py" not in record["markdown"]


async def test_model_failure_preserves_completed_batches(archive_client, app, settings, tmp_path):
    """第二批传输失败时第一批成果和进度继续可读，未处理文件保持未完成。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "first.py").write_text(python_route("/first"), encoding="utf-8")
    (directory / "last.py").write_text(
        "# context\n" * 2200 + python_route("/last"), encoding="utf-8"
    )
    model = SourceModel(fail_after=1)
    install_model(app, settings, model)
    imported = await import_project(client, directory)
    project_id = imported["project"]["id"]
    started = await start_document(client, project_id, imported["source"]["id"])
    record = await finished_document(client, app, project_id, started["id"])
    assert record["status"] == "failed"
    assert record["processed_files"] == record["processed_batches"] == 1
    assert "model_connection_failed" in record["error"]
    first = next(item for item in record["endpoints"] if item["path"] == "/first")
    assert first["summary"] == "源码声明的接口"
    assert "/last" in record["markdown"]


async def test_cancel_running_generation_rejects_duplicate_then_allows_new_run(
    archive_client, app, settings, tmp_path
):
    """同项目并发生成返回冲突，取消等待中的模型后可立即重新发起。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "routes.py").write_text(python_route(), encoding="utf-8")
    model = SourceModel(block=True)
    install_model(app, settings, model)
    imported = await import_project(client, directory)
    project_id = imported["project"]["id"]
    source_id = imported["source"]["id"]
    started = await start_document(client, project_id, source_id)
    async with asyncio.timeout(5):
        await model.entered.wait()
    duplicate = await client.post(
        f"/api/projects/{project_id}/documents", json={"source_id": source_id}
    )
    assert duplicate.status_code == 409
    cancelled = await client.post(f"/api/projects/{project_id}/documents/{started['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["processed_files"] == 0
    assert not app.state.documents.active
    again = await client.post(f"/api/projects/{project_id}/documents/{started['id']}/cancel")
    assert again.json()["status"] == "cancelled"
    model.block = False
    next_run = await start_document(client, project_id, source_id)
    record = await finished_document(client, app, project_id, next_run["id"])
    assert record["status"] == "completed"


async def test_incomplete_scan_and_empty_model_details_remain_partial(
    archive_client, app, settings, tmp_path
):
    """超限源码可见为扫描缺口，模型仅回声路由也不能成为完整生成结果。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "routes.py").write_text(python_route(), encoding="utf-8")
    (directory / "too_large.py").write_text("#" * (512 * 1024 + 1), encoding="utf-8")
    imported = await import_project(client, directory)
    assert imported["source"]["complete"] is False
    project_id = imported["project"]["id"]
    install_model(app, settings, SourceModel())
    started = await start_document(client, project_id, imported["source"]["id"])
    record = await finished_document(client, app, project_id, started["id"])
    assert record["status"] == "partial"
    assert record["scan_complete"] is False
    assert any(item["path"] == "too_large.py" for item in record["issues"])
    (directory / "too_large.py").write_text("# small\n", encoding="utf-8")
    refreshed = await client.post(
        f"/api/projects/{project_id}/source", json={"directory": str(directory)}
    )
    install_model(app, settings, SourceModel(empty=True))
    started = await start_document(client, project_id, refreshed.json()["id"])
    record = await finished_document(client, app, project_id, started["id"])
    assert record["status"] == "partial"
    assert PENDING in record["endpoints"][0]["uncertainties"]


async def test_recovery_marks_orphaned_generation_interrupted(archive_client, app, tmp_path):
    """仓储恢复保留已保存的接口和覆盖进度，未结束任务转换为中断。"""
    client = archive_client
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "routes.py").write_text(python_route(), encoding="utf-8")
    imported = await import_project(client, directory)
    project_id = imported["project"]["id"]
    source_id = imported["source"]["id"]
    from api_agent.infrastructure.orm import new_id

    document_id = new_id()
    store = app.state.documents.store
    store.save(
        project_id,
        "documents",
        {
            "id": document_id,
            "project_id": project_id,
            "project_name": "Recovery",
            "source_id": source_id,
            "source_sha256": imported["source"]["sha256"],
            "status": "running",
            "created_at": "2026-11-17T00:00:00+00:00",
            "processed_files": 1,
            "total_files": 2,
            "processed_batches": 1,
            "total_batches": 2,
            "scan_complete": True,
            "endpoints": [
                DocumentEndpoint(
                    method="GET", path="/saved", source_files=["routes.py"], summary="已保存结果"
                ).model_dump()
            ],
            "warnings": [],
            "issues": [],
            "error": None,
        },
    )
    store.recover()
    response = await client.get(f"/api/projects/{project_id}/documents/{document_id}")
    record = response.json()
    assert response.status_code == 200
    assert record["status"] == "interrupted"
    assert record["processed_files"] == 1
    assert record["endpoints"][0]["path"] == "/saved"
    assert "中断" in record["markdown"]


def test_source_chunks_preserve_all_characters_and_boundary_context():
    """分批边界保留原始字符和相邻上下文，空文件也得到一次处理机会。"""
    content = "first\nsecond\n" + "x" * 20 + "\nlast\n"
    batches = source_batches(
        [{"path": "source.py", "content": content}, {"path": "empty.py", "content": ""}],
        size=12,
    )
    chunks = [item for batch in batches for item in batch if item["path"] == "source.py"]
    assert "".join(item["content"] for item in chunks) == content
    assert chunks[0]["content"] == "first\n"
    assert chunks[1]["preceding_context"] == "first\n"
    assert chunks[0]["following_context"].startswith("second\n")
    assert any(item["path"] == "empty.py" for batch in batches for item in batch)


def test_merge_keeps_boolean_values_and_marks_conflicting_contracts():
    """参数False值正确保存，多批相同字段冲突可见且不会静默覆盖前批证据。"""
    first = DocumentEndpoint(
        method="GET",
        path="/items",
        source_files=["first.py"],
        parameters=[{"name": "page", "location": "query", "required": False}],
    ).model_dump()
    second = DocumentEndpoint(
        method="GET",
        path="/items",
        source_files=["second.py"],
        parameters=[{"name": "page", "location": "query", "required": True}],
    ).model_dump()
    result = merge_endpoints([first], [second])[0]
    assert result["parameters"][0]["required"] is False
    assert any("required" in item for item in result["uncertainties"])
    assert result["source_files"] == ["first.py", "second.py"]
