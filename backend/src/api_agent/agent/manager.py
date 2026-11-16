"""管理独立于 HTTP 请求生命周期的 Agent 后台任务。

依赖 AgentRuntime 完成编排、AgentStore 持久化；仅负责容量、取消与停机回收。
"""

import asyncio

from api_agent.agent.runtime import AgentRuntime
from api_agent.agent.schemas import AgentRunRequest
from api_agent.agent.service import AgentError, AgentService
from api_agent.agent.store import AgentStore


class AgentManager:
    """持有运行 ID 与 asyncio 任务的映射，限制本进程并发并统一取消任务。"""

    def __init__(self, store: AgentStore, max_active: int = 4):
        """绑定产物存储及并发上限，初始化任务表和接收任务的状态。"""
        self.store = store
        self.max_active = max_active
        self.tasks: dict[str, asyncio.Task] = {}
        self.closing = False

    def start(self, agent: AgentService, request: AgentRunRequest):
        """校验容量并创建后台运行，返回已冻结输入的初始记录。

        停机或容量耗尽抛 AgentError；运行创建校验与持久化异常直接上抛。
        """
        if self.closing or len(self.tasks) >= self.max_active:
            raise AgentError("agent_capacity_exceeded")
        runtime = AgentRuntime(agent, self.store)
        record = runtime.create(request)
        run_id = record["id"]
        task = asyncio.create_task(runtime.run(record, request), name=f"api-agent:{run_id}")
        self.tasks[run_id] = task

        def done(completed):
            """移除完成任务并取出其异常，避免后台持久化失败成为未检索异常。"""
            self.tasks.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()  # Retrieve exceptional persistence failures, if any.

        task.add_done_callback(done)
        return record

    async def cancel(self, run_id: str):
        """按 ID 读取记录、取消本进程任务并返回最终记录。

        尚未启动的协程由此补写取消状态；无效或不存在的 ID 由存储层报错。
        """
        record = self.store.read(run_id)
        task = self.tasks.get(run_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(run_id, None)
            record = self.store.read(run_id)
            # Cancellation before the coroutine's first instruction has no finally block.
            if record["status"] in {"queued", "running"}:
                record.update(status="cancelled", stop_reason="cancelled", report_status="partial")
                self.store.save(record)
        return record

    async def shutdown(self):
        """停止接收新运行，依次取消并等待当前全部后台任务退出。"""
        self.closing = True
        for run_id in list(self.tasks):
            await self.cancel(run_id)
