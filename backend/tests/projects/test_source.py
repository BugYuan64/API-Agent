"""Regression tests for complete project snapshots and declaration evidence."""

import os

import pytest

from api_agent.projects.source import (
    ScanLimits,
    SourceInput,
    SourceScanError,
    normalize_path,
    sanitize_source,
    scan_directory,
    snapshot_files,
)


def test_directory_scan_retains_all_sources_and_reports_exclusions(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "routes.py").write_text(
        'from fastapi import APIRouter\nrouter = APIRouter(prefix="/api")\n'
        '@router.get("/items")\ndef items():\n    return []\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "models.py").write_text("class Item:\n    pass\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text('app.get("/dependency", handler)')
    (tmp_path / ".env").write_text("TOKEN=never-read-this")
    (tmp_path / "image.png").write_bytes(b"\x00\x01")
    snapshot = scan_directory(tmp_path)
    assert snapshot.complete
    assert snapshot.scanned_files == 2
    assert snapshot.total_files == 4
    assert {(item.method, item.path) for item in snapshot.endpoints} == {("GET", "/api/items")}
    assert {item.reason for item in snapshot.issues} == {
        "excluded_directory",
        "credential_or_lock_file",
        "unsupported_file_type",
    }
    assert "never-read-this" not in snapshot.model_dump_json()
    assert snapshot.sha256 == scan_directory(tmp_path).sha256


@pytest.mark.parametrize(
    "path", ["../routes.py", "/routes.py", "C:/routes.py", "a/../routes.py", "a//b.py", "a\x00.py"]
)
def test_relative_upload_paths_cannot_escape(path):
    with pytest.raises(SourceScanError, match="invalid_source_path"):
        normalize_path(path)


def test_duplicate_upload_paths_are_rejected():
    with pytest.raises(SourceScanError, match="duplicate_source_path"):
        snapshot_files(
            [SourceInput(path="src/a.py", content=""), SourceInput(path="SRC/A.py", content="")]
        )


@pytest.mark.parametrize(
    "limits,reason",
    [
        (ScanLimits(max_file_bytes=12), "file_size_limit"),
        (ScanLimits(max_total_bytes=12), "project_size_limit"),
        (ScanLimits(max_files=1), "project_size_limit"),
        (ScanLimits(max_entries=1), "entry_limit"),
    ],
)
def test_eligible_source_over_limits_is_explicitly_partial(limits, reason):
    snapshot = snapshot_files(
        [
            SourceInput(path="a.py", content="value = 'hello world'\n"),
            SourceInput(path="b.py", content="second = True\n"),
        ],
        limits=limits,
    )
    assert not snapshot.complete
    assert reason in {item.reason for item in snapshot.issues}
    assert any("不得标记为完整" in item for item in snapshot.warnings)


def test_unsupported_encoding_is_an_explicit_gap(tmp_path):
    (tmp_path / "routes.py").write_bytes(b"\xff\xfe\x00")
    snapshot = scan_directory(tmp_path)
    assert not snapshot.complete
    assert snapshot.issues[0].reason == "unsupported_encoding"


def test_symbolic_link_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "routes.py").write_text('app.get("/secret", handler)')
    root = tmp_path / "project"
    root.mkdir()
    try:
        os.symlink(outside, root / "linked", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links requires OS permission")
    snapshot = scan_directory(root)
    assert not snapshot.complete
    assert snapshot.scanned_files == 0
    assert snapshot.issues[0].reason == "symbolic_link"


def test_python_router_mounts_across_files_preserve_prefixes_and_line_evidence():
    snapshot = snapshot_files(
        [
            SourceInput(
                path="server/main.py",
                content="""from fastapi import FastAPI
from server.routes import router as items_router
app = FastAPI()
app.include_router(items_router, prefix="/v1")
app.include_router(items_router, prefix="/v2")
""",
            ),
            SourceInput(
                path="server/routes.py",
                content="""from fastapi import APIRouter
router = APIRouter(prefix="/items")
@router.api_route(
    "/{item_id}", methods=["GET", "PATCH"]
)
async def item(item_id: str):
    return item_id
@router.websocket("/updates")
async def updates(socket):
    pass
""",
            ),
        ]
    )
    assert {(item.method, item.path, item.line) for item in snapshot.endpoints} == {
        ("GET", "/v1/items/{item_id}", 3),
        ("PATCH", "/v1/items/{item_id}", 3),
        ("GET", "/v2/items/{item_id}", 3),
        ("PATCH", "/v2/items/{item_id}", 3),
        ("WEBSOCKET", "/v1/items/updates", 8),
        ("WEBSOCKET", "/v2/items/updates", 8),
    }
    assert all(item.file == "server/routes.py" for item in snapshot.endpoints)


def test_flask_blueprint_prefix_and_explicit_methods():
    snapshot = snapshot_files(
        [
            SourceInput(
                path="app.py",
                content="""from flask import Flask, Blueprint
app = Flask(__name__)
bp = Blueprint("items", __name__, url_prefix="/items")
@bp.route("/<int:item_id>", methods=["GET", "DELETE"])
def item(item_id):
    pass
app.register_blueprint(bp, url_prefix="/api")
""",
            )
        ]
    )
    assert {(item.method, item.path) for item in snapshot.endpoints} == {
        ("GET", "/api/{item_id}"),
        ("DELETE", "/api/{item_id}"),
    }


def test_spring_multiline_class_mapping_arrays_and_request_methods():
    snapshot = snapshot_files(
        [
            SourceInput(
                path="ItemController.java",
                content="""package sample;
@RestController
@RequestMapping(
    value = {"/api/items", "/v2/items"}
)
public class ItemController {
    @GetMapping(path = {"", "/list"}, produces = "application/json")
    public List<Item> list() { return null; }
    @RequestMapping(
        path = "/{id}",
        method = {RequestMethod.PUT, RequestMethod.PATCH}
    )
    public Item update() { return null; }
}
""",
            )
        ]
    )
    assert {(item.method, item.path) for item in snapshot.endpoints} == {
        ("GET", "/api/items"),
        ("GET", "/api/items/list"),
        ("GET", "/v2/items"),
        ("GET", "/v2/items/list"),
        ("PUT", "/api/items/{id}"),
        ("PATCH", "/api/items/{id}"),
        ("PUT", "/v2/items/{id}"),
        ("PATCH", "/v2/items/{id}"),
    }
    assert {item.line for item in snapshot.endpoints} == {7, 9}


def test_express_literal_mount_and_route_chain():
    snapshot = snapshot_files(
        [
            SourceInput(
                path="routes.ts",
                content="""const router = express.Router();
router.get('/items/:id', read);
router.route('/items').get(list).post(create);
app.use('/api', router);
""",
            )
        ]
    )
    assert {(item.method, item.path) for item in snapshot.endpoints} == {
        ("GET", "/api/items/{id}"),
        ("GET", "/api/items"),
        ("POST", "/api/items"),
    }


def test_dynamic_and_unparseable_source_is_kept_for_ai_with_warnings():
    files = [
        SourceInput(
            path="router.py", content='@app.get(PREFIX + "/items")\ndef items():\n    pass\n'
        ),
        SourceInput(path="new_syntax.py", content="def invalid(: pass\n"),
        SourceInput(path="routes.go", content="package main\nfunc main() {}\n"),
    ]
    snapshot = snapshot_files(files)
    assert snapshot.complete
    assert snapshot.scanned_files == 3
    assert snapshot.endpoints == []
    assert any("动态表达式" in item for item in snapshot.warnings)
    assert any("静态语法分析" in item for item in snapshot.warnings)


def test_complete_source_text_is_redacted_without_truncation():
    text = "# explanation\n" * 5000 + 'API_KEY = "sk-abcdefghijklmnop"\nmarker = "END"\n'
    snapshot = snapshot_files([SourceInput(path="config.py", content=text)])
    assert snapshot.complete
    assert len(snapshot.files[0].content) > 60000
    assert 'marker = "END"' in snapshot.files[0].content
    assert "sk-abcdefghijklmnop" not in snapshot.model_dump_json()
    assert "[REDACTED]" in snapshot.files[0].content


def test_redaction_preserves_types_and_dynamic_credential_expressions():
    text = """from pydantic import BaseModel
import os
class Login(BaseModel):
    password: str
    token: str | None = None
    api_key: str = os.getenv("MODEL_API_KEY")
    name: str
token = os.getenv("TARGET_TOKEN")
password = "str"
actual_password = "private-value"
echo = 'private-value'
"""
    result = sanitize_source(text)
    assert "password: str" in result
    assert "token: str | None = None" in result
    assert 'api_key: str = os.getenv("MODEL_API_KEY")' in result
    assert 'token = os.getenv("TARGET_TOKEN")' in result
    assert "name: str" in result
    assert 'password = "[REDACTED]"' in result
    assert "private-value" not in result
    assert result.count("\n") == text.count("\n")


def test_redaction_cleans_config_scalars_and_blocks_without_losing_lines():
    text = """server:
  port: 8000
credentials:
  password: plain-secret
  token: ${TARGET_TOKEN}
  api_key: |
    private-line-one
    private-line-two
  name: sample
"""
    result = sanitize_source(text, path="application.yaml")
    assert "plain-secret" not in result
    assert "private-line" not in result
    assert "${TARGET_TOKEN}" in result
    assert "name: sample" in result
    assert result.count("\n") == text.count("\n")


def test_sanitized_snapshot_keeps_cross_file_types_and_redacts_literal_echoes():
    snapshot = snapshot_files(
        [
            SourceInput(path="config.py", content='password = "str"\ntoken = "hidden-value"\n'),
            SourceInput(path="models.py", content="password: str\necho = 'hidden-value'\n"),
        ]
    )
    assert "password: str" in snapshot.files[1].content
    assert "hidden-value" not in snapshot.model_dump_json()


def test_runtime_artifacts_are_excluded_but_business_data_source_is_scanned(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "routes.py").write_text(
        "from fastapi import APIRouter\nrouter=APIRouter()\n"
        '@router.get("/data")\ndef read(): pass\n',
        encoding="utf-8",
    )
    artifacts = data / "artifacts"
    artifacts.mkdir()
    (artifacts / "snapshot.json").write_text('{"nested_snapshot":"runtime"}')
    temporary = tmp_path / ".pytest-temporary"
    temporary.mkdir()
    (temporary / "routes.py").write_text("runtime = True")
    snapshot = scan_directory(tmp_path, exclude_paths=[artifacts, tmp_path.parent / "unrelated"])
    assert snapshot.complete
    assert [item.path for item in snapshot.files] == ["data/routes.py"]
    assert {(item.path, item.reason) for item in snapshot.issues} == {
        ("data/artifacts", "excluded_runtime_directory"),
        (".pytest-temporary", "excluded_directory"),
    }


def test_test_fixture_routes_are_explicitly_qualified():
    snapshot = snapshot_files(
        [
            SourceInput(
                path="tests/fixtures/routes.py",
                content="""from fastapi import FastAPI
app = FastAPI()
@app.get("/test-only")
def test_route(): pass
""",
            ),
        ]
    )
    assert len(snapshot.endpoints) == 1
    assert "此声明位于测试/示例源码" in snapshot.endpoints[0].warnings[0]


def test_express_client_calls_are_not_service_routes_and_nested_mounts_expand():
    snapshot = snapshot_files(
        [
            SourceInput(
                path="server.js",
                content="""const app = express();
const router = express.Router();
const inner = express.Router();
app.use('/api', router);
router.use('/v1', inner);
inner.get('/items', handler);
client.get('/remote/items');
axios.post('/remote/items', payload);
""",
            ),
        ]
    )
    assert {(item.method, item.path) for item in snapshot.endpoints} == {("GET", "/api/v1/items")}


def test_credential_controller_source_is_retained_while_secret_config_is_excluded():
    snapshot = snapshot_files(
        [
            SourceInput(path="credentials.py", content="password: str\n"),
            SourceInput(path="credentials.json", content='{"password":"private-value"}'),
        ]
    )
    assert [item.path for item in snapshot.files] == ["credentials.py"]
    assert snapshot.issues[0].reason == "credential_or_lock_file"
