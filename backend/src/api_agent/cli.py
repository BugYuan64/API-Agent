"""Independent, deterministic test entry point. No LLM is called in this phase."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from api_agent.infrastructure.config import ConfigurationError, load_settings
from api_agent.tools.client import call_tool


async def execute(
    command: str,
    plan_path: Path | None,
    allowed_origins: list[str] | None,
    engine: str | None = None,
) -> dict:
    """按子命令准备输入并通过 MCP 调用对应工具。

    作用：把 CLI 的三个命令（doctor/validate/run）映射到三个 MCP 工具，
    中间不做任何策略判断——策略全在 MCP 子进程里。
    实现：
    ① ``--allow-origin`` 只是本次调用的显式授权，因此仅在非空时作为
       ``target_allowed_origins`` 覆盖项传给 ``load_settings``，
       不写回配置文件，避免权限被持久化；
    ② ``doctor`` 不需要载荷；其余命令要求计划文件存在且不超过 256KiB，
       读取时用 ``utf-8-sig`` 容忍 BOM；
       任何 OSError/JSON 解析错误都折叠成 ``invalid_plan_file``；
    ③ ``--engine`` 透传给 MCP 工具，缺省由服务端选择默认驱动；
    ④ 用字典把命令名映射到工具名后调用 ``call_tool``。
    """
    settings = load_settings(
        **({"target_allowed_origins": allowed_origins} if allowed_origins else {})
    )
    payload = None
    if command != "doctor":
        try:
            if plan_path is None or plan_path.stat().st_size > 262_144:
                raise ValueError
            payload = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {"status": "error", "error_kind": "invalid_plan_file"}
    names = {"doctor": "postman_status", "validate": "validate_plan", "run": "run_plan"}
    return await call_tool(settings, names[command], payload, engine)


def exit_code(result: dict) -> int:
    """把工具结果映射成进程退出码。

    作用：让 CLI 能被 shell 脚本和 CI 直接判定成败。
    实现：三种“成功”形态（CLI 可用 / 计划合法 / 执行通过）都返回 0；
    明确失败（``status == "failed"``，即断言确实没通过）返回 1；
    其余情况（配置错误、CLI 缺失、超时、报告无效等）返回 2，
    以便区分“测试红”和“测试没跑起来”。
    """
    if (
        result.get("available") is True
        or result.get("valid") is True
        or result.get("status") == "passed"
    ):
        return 0
    return 1 if result.get("status") == "failed" else 2


def main() -> None:
    """CLI 入口：解析参数、执行、输出 JSON、设置退出码。

    实现：argparse 定义全局 ``--allow-origin``（可重复）、``--engine``
    与三个子命令（doctor / validate <plan> / run <plan>）；执行过程按异常分层兜底——
    ``KeyboardInterrupt`` → cancelled，``ConfigurationError`` → invalid_configuration，
    其它异常统一 → mcp_connection_error（不打印堆栈，避免泄露宿主细节）；
    结果始终以单行 JSON 打印到标准输出，最后用 ``exit_code`` 决定退出状态。
    """
    parser = argparse.ArgumentParser(
        description="Run controlled API tests via local MCP with pluggable engines"
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        help="Explicit local origin permission for this invocation",
    )
    parser.add_argument(
        "--engine",
        choices=["native", "postman"],
        default=None,
        help="Execution engine: native in-memory HTTPX or Postman CLI sandbox",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check engine availability over MCP")
    for name in ("validate", "run"):
        command = commands.add_parser(name)
        command.add_argument("plan", type=Path)
    arguments = parser.parse_args()
    try:
        result = asyncio.run(
            execute(
                arguments.command,
                getattr(arguments, "plan", None),
                arguments.allow_origin,
                arguments.engine,
            )
        )
    except KeyboardInterrupt:
        result = {"status": "cancelled", "error_kind": "cancelled"}
    except ConfigurationError:
        result = {"status": "error", "error_kind": "invalid_configuration"}
    except Exception:
        result = {"status": "error", "error_kind": "mcp_connection_error"}
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(exit_code(result))


if __name__ == "__main__":
    main()
