import json

import pytest

from api_agent.infrastructure.config import load_settings
from api_agent.tools.client import call_tool, open_client


def payload(origin="http://127.0.0.1:8080"):
    return {
        "origin": origin,
        "allowed_methods": ["POST"],
        "path_templates": ["/api/user/login"],
        "requests": [
            {
                "id": "invalid-login",
                "method": "POST",
                "path": "/api/user/login",
                "body": {},
                "assertions": [{"kind": "status", "expected": 200}],
            }
        ],
    }


async def test_real_stdio_initialization_tools_validation_and_safe_errors(tmp_path):
    settings = load_settings(
        env_file=None, project_root=tmp_path, target_allowed_origins=["http://127.0.0.1:8080"]
    )
    async with open_client(settings) as client:
        listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == {
            "postman_status",
            "validate_plan",
            "run_plan",
        }
        assert client.server_info.name == "API-Agent Postman"
        result = await client.call_tool("validate_plan", {"payload": payload()})
        assert result.structured_content["valid"] is True
        bad = payload()
        bad["raw_script"] = "PRIVATE_UNTRUSTED"
        result = await client.call_tool("validate_plan", {"payload": bad})
        assert result.structured_content == {"valid": False, "error_kind": "invalid_plan"}
        assert "PRIVATE" not in json.dumps(result.model_dump())
        result = await client.call_tool("run_plan", {"payload": payload()})
        assert result.structured_content["error_kind"] == "cli_unavailable"


async def test_independent_client_denies_unconfigured_origin(tmp_path):
    settings = load_settings(env_file=None, project_root=tmp_path)
    result = await call_tool(settings, "validate_plan", payload())
    assert result == {"valid": False, "error_kind": "origin_not_allowed"}


async def test_secret_reference_requires_server_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "DO_NOT_FORWARD")
    settings = load_settings(
        env_file=None, project_root=tmp_path, target_allowed_origins=["http://127.0.0.1:8080"]
    )
    data = payload()
    data["auth_ref"] = {"kind": "header", "header_name": "token", "env_var": "PRIVATE_KEY"}
    assert await call_tool(settings, "run_plan", data) == {
        "status": "error",
        "error_kind": "auth_reference_not_allowed",
    }


@pytest.mark.parametrize("data", ["PRIVATE_INVALID", None, 123, {"origin": "PRIVATE_BAD"}])
async def test_malformed_tool_input_is_not_echoed(tmp_path, data):
    settings = load_settings(env_file=None, project_root=tmp_path)
    result = await call_tool(settings, "validate_plan", data)
    assert result == {"valid": False, "error_kind": "invalid_plan"}
