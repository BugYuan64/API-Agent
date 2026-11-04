"""Test engine abstraction (Driver / Adapter Pattern).

Decouples the declarative ExecutionPlan from the concrete execution technology
(Native Python in-memory engine vs. Postman CLI sandbox engine vs. external MCP).
"""

from typing import Any, Protocol

from api_agent.execution.schemas import ExecutionPlan, RunResult


class TestEngine(Protocol):
    """测试执行驱动抽象契约。

    作用：将上层测试计划（ExecutionPlan）与底层具体发包或沙箱执行技术彻底解耦。
    任何引擎实现只需要满足此契约，即可无缝接入控制台、独立 CLI 以及 MCP 协议。
    """

    name: str

    async def status(self) -> dict[str, Any]:
        """探测当前引擎在本地环境中的就绪状态与元信息。"""
        ...

    async def run(self, plan: ExecutionPlan, secrets: dict[str, str]) -> RunResult:
        """执行一份受控执行计划并输出归一化运行结果。"""
        ...
