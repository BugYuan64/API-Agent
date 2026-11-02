"""Single-process worker. SQLite owns the queue; an event only wakes the loop."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.database import Database
from api_agent.infrastructure.orm import as_utc, utc_now
from api_agent.runs.models import Run
from api_agent.runs.repository import RunRepository
from api_agent.runs.state_machine import CANCELLING

logger = logging.getLogger("api_agent")


class RunWorker:
    """单进程的后台执行器：轮询 SQLite 队列并逐个跑完运行。

    作用：把“串行执行 + 可外部唤醒 + 可优雅停止”集中在一个 asyncio 任务里，
    与 HTTP 请求处理解耦。
    实现：``wakeup`` 事件用于低延迟唤醒（创建/取消后立即通知），
    平时靠 ``poll_interval`` 的短超时兜底轮询；
    真正的抢占由数据库层的条件更新完成，因此本类不需要锁。
    """

    def __init__(self, database: Database, *, poll_interval: float = 0.05):
        """初始化 worker 的配置与内部状态（不启动任务）。

        实现：``wakeup`` 是用于即时唤醒的 asyncio 事件；
        ``task`` 初始为 None，由 ``start`` 惰性创建，便于 ``stop`` 后再启动；
        ``active_run_id`` 仅用于观测当前在跑哪个运行，不参与调度判断。
        """
        self.database = database
        self.poll_interval = poll_interval
        self.wakeup = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.active_run_id: str | None = None

    async def recover(self) -> int:
        """启动时的崩溃恢复：释放未派发的预留，并把在途运行标记为中断。

        作用：保证进程重启后账本与运行状态不出现“悬空的预留”或“永远 running”。
        实现：开一个独立会话，先 ``release_unstarted_reservations``（从未派发的 pending
        尝试归还预算），再 ``record_interrupted``（在途尝试记为 unknown、运行置为
        interrupted），返回被中断的运行数供启动日志使用。
        """
        async with self.database.session() as session:
            repository = RunRepository(session)
            await repository.release_unstarted_reservations()
            return await repository.record_interrupted()

    def start(self) -> None:
        """启动后台轮询任务（幂等）。

        实现：仅当 ``self.task is None`` 时创建 asyncio 任务并命名，
        重复调用不会产生第二个循环；任务在 lifespan 结束时由 ``stop`` 取消。
        """
        if self.task is None:
            self.task = asyncio.create_task(self._loop(), name="api-agent-run-worker")

    def notify(self) -> None:
        """立即唤醒轮询循环（有新任务入队或取消请求时调用）。

        实现：设置 asyncio.Event，正在等待的 ``wait_for`` 会马上返回，
        从而不必等到下一次轮询间隔。
        """
        self.wakeup.set()

    async def stop(self) -> None:
        """取消并等待后台任务结束。

        实现：``task.cancel()`` 后 ``await`` 该任务并吞掉 ``CancelledError``
        （取消是预期行为，不是错误）；最后把 ``self.task`` 复位为 None，
        便于测试中再次 ``start``。
        """
        if self.task is None:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        finally:
            self.task = None

    async def _loop(self) -> None:
        """主循环：反复“取一个可认领的运行 → 执行 → 回到取任务”。

        实现细节：
        ① 用独立会话查 ``list_queued`` 拿候选 ID 列表（此时不持有长事务）；
        ② 对每个候选再开一次会话，读取快照里的 ``work_timeout_ms`` 计算 deadline，
           然后调用 ``claim`` 做原子抢占；抢到就跳出循环开始执行；
        ③ 一个都没抢到：先 ``clear()`` 再 ``wait_for`` 等待唤醒或超时——
           先 clear 再 wait 避免“通知早于等待”导致的丢失唤醒；
        ④ 执行期异常统一兜底：记录日志并把运行推进到 ``failed``；
        ⑤ ``CancelledError`` 直接向上抛（不吞），让 recovery 在下次启动时把在途工作记为 unknown。
        """
        while True:
            run_id = None
            async with self.database.session() as session:
                queued = await RunRepository(session).list_queued()
            for candidate in queued:
                async with self.database.session() as session:
                    run = await session.get(Run, candidate)
                    timeout = (
                        run.snapshot.resource_limits.get("work_timeout_ms", 60_000)
                        if run
                        else 60_000
                    )
                    claimed = await RunRepository(session).claim(
                        candidate, utc_now() + timedelta(milliseconds=timeout)
                    )
                if claimed:
                    run_id = candidate
                    break
            if run_id is None:
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=self.poll_interval)
                except TimeoutError:
                    pass
                continue
            self.active_run_id = run_id
            try:
                await self.execute(run_id)
            except asyncio.CancelledError:
                # Recovery marks in-flight work unknown at the next startup.
                raise
            except Exception:
                logger.exception("Simulation run failed", extra={"context": {"run_id": run_id}})
                async with self.database.session() as session:
                    await RunRepository(session).finish(
                        run_id, "failed", "workflow_error", "Simulation workflow failed"
                    )
            finally:
                self.active_run_id = None

    async def execute(self, run_id: str) -> None:
        """按快照逐个“执行”动作，直到完成、失败、取消或超时。

        作用：这是运行的核心状态推进循环，每次迭代只处理一个动作，
        每个阶段用独立短事务，避免长事务锁库。
        实现：每轮先读最新 run 状态并按优先级判断四种收尾条件——
        ① 已被请求取消 → 记事件并落 ``cancelled``；
        ② 状态已不是 running → 直接返回；
        ③ 超过 deadline → 落 ``completed``（stop_reason=deadline_exceeded）；
        ④ 动作已全部执行 → 落 ``completed``；
        否则取出下一个动作，用 ``begin_attempt`` 原子地检查预算并预留；
        预留失败说明预算耗尽，落 ``completed``（action_budget_exhausted）。
        然后 sleep 模拟动作耗时，再开新会话结算：``finish_attempt`` 把预留转消耗、
        ``current_action`` 自增、追加结果事件；若动作报告失败且未被取消，
        直接把运行置为 ``failed`` 并写结束时间。
        """
        while True:
            async with self.database.session() as session:
                repository = RunRepository(session)
                run = await repository.get(run_id)
                if run.status == CANCELLING:
                    await self._finish_cancelled(session, run_id)
                    return
                if run.status != "running":
                    return
                if utc_now() >= as_utc(run.deadline):
                    await repository.finish(run_id, "completed", "deadline_exceeded")
                    return
                if run.current_action >= run.action_count:
                    await repository.finish(run_id, "completed", "completed")
                    return
                snapshot = await repository.get_snapshot(run.snapshot_id)
                action = snapshot.actions[run.current_action]
                attempt = await repository.begin_attempt(
                    run, action, snapshot.resource_limits.get("max_actions", 100)
                )
                if attempt is None:
                    await repository.finish(run_id, "completed", "action_budget_exhausted")
                    return
            try:
                await asyncio.sleep(attempt_duration(action))
            except asyncio.CancelledError:
                raise
            async with self.database.session() as session:
                repository = RunRepository(session)
                # 重新挂回本会话：attempt 来自已关闭的上一个会话，不能直接 add/commit。
                attempt = await session.merge(attempt)
                await repository.finish_attempt(attempt, action["outcome"])
                run = await repository.get(run_id)
                run.current_action += 1
                cancelled = run.status == CANCELLING
                await repository.add_event(
                    run_id,
                    node="execute",
                    event_type="completed" if action["outcome"] == "completed" else "failed",
                    status="completed" if not cancelled else "cancelling",
                    tool_name="simulation_action",
                    observation_summary={
                        "outcome": action["outcome"],
                        "sequence": run.current_action,
                    },
                    decision_reason=(
                        "Cancellation requested; no further action will start"
                        if cancelled
                        else "Deterministic local action recorded"
                    ),
                    budget_summary={"resource": "simulation_action", "consumed": 1},
                )
                if action["outcome"] == "failed" and not cancelled:
                    run.status = "failed"
                    run.stop_reason = "workflow_error"
                    run.workflow_error = "A simulation action reported failure"
                    run.ended_at = utc_now()
                await session.commit()

    async def _finish_cancelled(self, session: AsyncSession, run_id: str) -> None:
        """把处于 cancelling 的运行真正收尾成 cancelled。

        作用：调用方的取消请求只把状态改成 ``cancelling``，
        由执行循环在安全点（两个动作之间）落终态，保证不会中途打断已开始的动作。
        实现：先记一条 ``cancelled`` 事件并提交（让证据先落库），
        再用 ``finish`` 的条件更新把状态推进到终态。
        """
        repository = RunRepository(session)
        await repository.add_event(
            run_id,
            node="execute",
            event_type="cancelled",
            status="cancelled",
            decision_reason="Cancellation completed; in-flight local action was not replayed",
        )
        await session.commit()
        await repository.finish(run_id, "cancelled", "cancelled")


def attempt_duration(action: dict) -> float:
    """把动作的快照毫秒时长换算成 ``asyncio.sleep`` 需要的秒数。

    实现：``duration_ms / 1000``；用 sleep 代替真实请求，
    让模拟动作的耗时可控且可复现（配合 random_seed 保证确定性）。
    """
    return action["duration_ms"] / 1000
