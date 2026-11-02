"""The independent application uses MCP, rather than calling tools in-process."""

import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp import Client, StdioServerParameters

from api_agent.infrastructure.config import Settings

ToolName = Literal["postman_status", "validate_plan", "run_plan"]


@asynccontextmanager
async def open_client(settings: Settings):
    """以子进程方式拉起本地 MCP 服务器并建立客户端连接。

    作用：应用与被调用的工具之间保持进程隔离——工具运行在一个独立 Python 进程里，
    因此即使执行环节出问题也不会污染主进程。
    实现：
    ① 显式构造子进程环境：只传项目根、允许的 origin/凭据变量白名单、CLI 路径
       和几个编码/缓冲相关变量，不整体继承 ``os.environ``；
    ② 只把白名单里确实存在的鉴权变量透传给子进程，避免其它环境变量（可能含
       无关密钥）被一并带过去；
    ③ 用 ``sys.executable -m api_agent.tools.server`` 启动，保证用的是当前解释器
       与已安装的包；``read_timeout_seconds=140`` 给足执行计划的最长耗时余量。
    """
    environment = {
        "PROJECT_ROOT": str(settings.project_root),
        "TARGET_ALLOWED_ORIGINS": json.dumps(settings.target_allowed_origins),
        "TARGET_AUTH_ENV_VARS": json.dumps(settings.target_auth_env_vars),
        "POSTMAN_CLI_PATH": str(settings.postman_cli_path) if settings.postman_cli_path else "",
        "OMIT_RESPONSE_PAYLOAD": str(settings.omit_response_payload).lower(),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "LOG_LEVEL": "ERROR",
    }
    for name in settings.target_auth_env_vars:
        if name in os.environ:
            environment[name] = os.environ[name]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "api_agent.tools.server"],
        cwd=settings.project_root,
        env=environment,
    )
    async with Client(parameters, read_timeout_seconds=140) as client:
        yield client


async def call_tool(settings: Settings, name: ToolName, payload: Any = None) -> dict:
    """通过 MCP 调用一个工具并返回其结构化结果。

    作用：CLI 与 MCP 之间的唯一通道，保证“工具执行”始终发生在独立进程里。
    实现：先用白名单再校验一次工具名（防止调用方传入任意方法名），
    然后每次调用都新建客户端会话——用完即关，不长期持有子进程；
    ``postman_status`` 不接受参数，其余工具统一把 ``payload`` 包进 ``{"payload": ...}``；
    若工具返回错误标志或结构化内容不是 dict，统一折叠成 ``mcp_tool_error``，
    避免把非结构化文本透传给上层。
    """
    if name not in {"postman_status", "validate_plan", "run_plan"}:
        return {"status": "error", "error_kind": "unknown_tool"}
    async with open_client(settings) as client:
        result = await client.call_tool(
            name, {} if name == "postman_status" else {"payload": payload}
        )
        if result.is_error or not isinstance(result.structured_content, dict):
            return {"status": "error", "error_kind": "mcp_tool_error"}
        return result.structured_content
