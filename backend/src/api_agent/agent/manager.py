"""Own background jobs independently of HTTP request/session lifetimes."""

import asyncio

from api_agent.agent.runtime import AgentRuntime
from api_agent.agent.schemas import AgentRunRequest
from api_agent.agent.service import AgentError, AgentService
from api_agent.agent.store import AgentStore


class AgentManager:
    def __init__(self, store: AgentStore, max_active: int = 4):
        self.store = store
        self.max_active = max_active
        self.tasks: dict[str, asyncio.Task] = {}
        self.closing = False

    def start(self, agent: AgentService, request: AgentRunRequest):
        if self.closing or len(self.tasks) >= self.max_active:
            raise AgentError("agent_capacity_exceeded")
        runtime = AgentRuntime(agent, self.store)
        record = runtime.create(request)
        run_id = record["id"]
        task = asyncio.create_task(runtime.run(record, request), name=f"api-agent:{run_id}")
        self.tasks[run_id] = task

        def done(completed):
            self.tasks.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()  # Retrieve exceptional persistence failures, if any.

        task.add_done_callback(done)
        return record

    async def cancel(self, run_id: str):
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
        self.closing = True
        for run_id in list(self.tasks):
            await self.cancel(run_id)
