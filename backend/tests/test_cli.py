import pytest

from api_agent.cli import execute, exit_code


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ({"status": "passed"}, 0),
        ({"valid": True}, 0),
        ({"available": True}, 0),
        ({"status": "failed"}, 1),
        ({"status": "error"}, 2),
        ({"valid": False}, 2),
    ],
)
def test_exit_codes_reflect_evidence(result, code):
    assert exit_code(result) == code


async def test_invalid_file_returns_safe_error_without_contacting_mcp(tmp_path):
    result = await execute("run", tmp_path / "private-missing-file.json", None)
    assert result == {"status": "error", "error_kind": "invalid_plan_file"}
