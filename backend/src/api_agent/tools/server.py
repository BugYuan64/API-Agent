"""Three bounded API tools over stdio; no shell or filesystem tools."""

from typing import Any

from mcp.server import MCPServer

from api_agent.execution.service import ExecutionService
from api_agent.infrastructure.config import Settings, load_settings


def create_server(settings: Settings) -> MCPServer:
    """构造本地 stdio MCP 服务器，只暴露三个受限工具。

    作用：让外部编排器（CLI 或后续 Agent）通过 MCP 协议调用执行能力，
    但工具集刻意只含“查状态、校验、执行”三件，且都是数据接口——
    没有 shell、没有文件读写、没有任意 HTTP，因此即使调用方被诱导也无法越界。
    实现：先按配置构造一个 ``ExecutionService``（策略即配置，进程内共享），
    再注册三个工具函数：``postman_status``（探测 CLI）、
    ``validate_plan``（只校验）、``run_plan``（执行）；
    两个异步工具都用 ``try/except Exception`` 兜底，确保任何内部异常
    都转成结构化错误而不是让 MCP 会话崩掉。
    """
    service = ExecutionService(settings)
    server = MCPServer(
        "API-Agent Postman",
        version="0.1.0",
        log_level="ERROR",
        instructions="Execute only data-only API test plans within local configured scope.",
    )

    @server.tool()
    async def postman_status() -> dict[str, Any]:
        """检查原生 Postman CLI 是否可用（不登录、不访问目标）。

        实现：委托 ``runner.status()`` 执行 ``--version`` 探测；
        捕获所有异常并返回 ``cli_unavailable``，保证工具调用永不抛错。
        """
        try:
            return await service.runner.status()
        except Exception:
            return {"available": False, "error_kind": "cli_unavailable"}

    @server.tool()
    def validate_plan(payload: Any) -> dict[str, Any]:
        """校验一份 v1 ExecutionPlan 及本地范围，不发送任何请求。

        实现：直接委托 ``service.validate``；它是同步的，
        因为整个流程只做 pydantic 校验和集合比较，没有 IO。
        """
        return service.validate(payload)

    @server.tool()
    async def run_plan(payload: Any) -> dict[str, Any]:
        """执行一份受限的 v1 ExecutionPlan，只返回断言元信息。

        实现：委托 ``service.run``（内部已做解析、范围校验、凭据解析与超时控制）；
        异常兜底返回 ``internal_execution_error``，避免把堆栈经 MCP 透出给调用方。
        """
        try:
            return await service.run(payload)
        except Exception:
            return {"status": "error", "error_kind": "internal_execution_error"}

    return server


def main() -> None:
    """MCP 服务器入口：装载配置并以 stdio 传输方式运行。

    实现：``load_settings`` 读取环境与 .env（失败会抛 ConfigurationError）；
    ``transport="stdio"`` 表示由宿主进程以子进程方式通过标准输入输出通信，
    不监听任何网络端口。
    """
    create_server(load_settings()).run(transport="stdio")


if __name__ == "__main__":
    main()
