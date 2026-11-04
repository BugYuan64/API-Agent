"""Atomic run transitions, event allocation and append-only resource accounting."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import conflict, not_found
from api_agent.infrastructure.orm import utc_now
from api_agent.runs.models import BudgetLedger, DispatchAttempt, Run, RunEvent, RunSnapshot
from api_agent.runs.state_machine import CANCELLING, RUNNING, sources_for


class RunRepository:
    """运行记录、事件、预算账本和派发尝试的持久化层。

    作用：把“状态迁移必须原子”“事件序号必须唯一”“账本只追加”这三条硬约束
    实现在 SQL 语句里，而不是在业务代码里靠先读后写。
    实现：绝大多数写操作都用 ``update(...).where(...)`` 的条件更新 + ``rowcount`` 判定，
    保证并发下只有一方成功；会话每次只承担一小段事务，方法内部自带 commit。
    """

    def __init__(self, session: AsyncSession):
        """持有会话；本类的写方法自带 commit，因此每次调用都是独立小事务。

        实现：与 ProjectRepository 不同，这里的多数方法末尾会显式提交——
        运行推进是长流程，若把事务拉长到整次执行会长时间占着 SQLite 写锁。
        """
        self.session = session

    async def add(self, snapshot: RunSnapshot, run: Run) -> None:
        """把运行快照和运行记录一起写入。

        实现：``add_all`` 后一次 commit，保证运行记录不会指向一个不存在的快照；
        快照的 ID 由调用方预先赋值，因此两条 INSERT 可以在同一事务里完成。
        """
        self.session.add_all([snapshot, run])
        await self.session.commit()

    async def get(self, run_id: str) -> Run | None:
        """按主键取运行记录（含通过 ``selectin`` 预加载的 snapshot）。

        实现：``session.get`` 命中身份映射；``Run.snapshot`` 关系声明了
        ``lazy="selectin"``，因此取 run 时会顺带把快照一并加载，
        避免异步上下文里再触发隐式懒加载。
        """
        return await self.session.get(Run, run_id)

    async def get_snapshot(self, snapshot_id: str) -> RunSnapshot | None:
        """按主键取运行快照（动作清单、资源上限、随机种子等）。

        实现：直接 ``session.get(RunSnapshot, id)``；快照一经创建不再修改。
        """
        return await self.session.get(RunSnapshot, snapshot_id)

    async def list(self, limit: int, offset: int) -> tuple[list[Run], int]:
        """分页列出运行记录，并返回总数。

        实现：两次查询（当页 select + count）；排序用 ``(created_at, id)``
        保证同秒创建时顺序稳定，翻页不重不漏。
        """
        runs = (
            await self.session.scalars(
                select(Run).order_by(Run.created_at, Run.id).limit(limit).offset(offset)
            )
        ).all()
        total = await self.session.scalar(select(func.count()).select_from(Run))
        return list(runs), total or 0

    async def list_queued(self) -> list[str]:
        """列出所有排队中运行的 ID（按创建时间先后）。

        作用：worker 只拿到 ID 而不再直接拿 ORM 对象，
        因为真正的“认领”要用独立的原子更新完成，先读到的状态可能已过期。
        实现：只 select ``Run.id``，条件 ``status == "queued"``，
        按 ``(created_at, id)`` 排序实现先来先服务。
        """
        return list(
            (
                await self.session.scalars(
                    select(Run.id).where(Run.status == "queued").order_by(Run.created_at, Run.id)
                )
            ).all()
        )

    async def claim(self, run_id: str, deadline: datetime) -> bool:
        """原子地把一个排队中的运行认领为 running。

        作用：多个 worker/进程同时抢同一任务时，只有一方能成功。
        实现：``UPDATE ... WHERE id=? AND status='queued'``，同时写入
        ``started_at`` 和根据工作超时算出的 ``deadline``；
        用 ``rowcount == 1`` 作为抢占成功的判据并提交。
        """
        result = await self.session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(status=RUNNING, started_at=utc_now(), deadline=deadline)
        )
        await self.session.commit()
        return result.rowcount == 1

    async def request_cancel(self, run_id: str) -> Run:
        """按当前状态处理取消请求。

        作用：取消是协作式的——worker 正在跑的动作不会被中断，只是不再启动新动作。
        实现：分三种情况——
        ① ``queued``：还没开始，直接落成 ``cancelled`` 终态并补一条事件；
        ② ``running``：只能置为 ``cancelling``，把真正收尾交给 worker；
        ③ ``cancelling``：重复取消请求幂等忽略；
        其余状态（终态）不做任何改动。最后统一 commit 并返回最新对象。
        """
        run = await self.get(run_id)
        if run is None:
            raise not_found("Run")
        now = utc_now()
        queued = await self.session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(
                status="cancelled", stop_reason="cancelled", cancel_requested_at=now, ended_at=now
            )
            .execution_options(synchronize_session=False)
        )
        if queued.rowcount == 1:
            await self.add_event(
                run_id,
                node="validate",
                event_type="cancelled",
                status="cancelled",
                decision_reason="Run was cancelled before it started",
            )
        else:
            await self.session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == RUNNING)
                .values(status=CANCELLING, cancel_requested_at=now)
                .execution_options(synchronize_session=False)
            )
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def finish(
        self, run_id: str, status: str, stop_reason: str, error: str | None = None
    ) -> bool:
        """把运行推进到终态。

        作用：以条件更新的方式落地终态，避免两个执行路径重复终结同一次运行。
        实现：先用 ``sources_for(status)`` 反查哪些状态允许进入该终态
        （非法目标直接 409）；再执行 ``UPDATE ... WHERE status IN (允许来源)``，
        同时写 stop_reason、ended_at 和可选的 workflow_error；
        ``rowcount == 1`` 表示本次迁移生效。
        """
        allowed = sources_for(status)
        if not allowed:
            raise conflict("invalid_run_transition", f"Cannot move a run into {status}")
        result = await self.session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.in_(allowed))
            .values(
                status=status,
                stop_reason=stop_reason,
                ended_at=utc_now(),
                workflow_error=error,
            )
        )
        await self.session.commit()
        return result.rowcount == 1

    async def add_event(
        self,
        run_id: str,
        *,
        node: str,
        event_type: str,
        status: str,
        tool_name: str | None = None,
        input_summary: dict | None = None,
        observation_summary: dict | None = None,
        decision_reason: str | None = None,
        budget_summary: dict | None = None,
    ) -> RunEvent:
        """追加一条运行事件，序号在运行内严格递增。

        作用：事件流是运行过程的可审计证据，必须保序且不重复。
        实现：先 ``SELECT MAX(sequence) ... WHERE run_id=?`` 取当前最大序号，再 +1；
        ``flush`` 而非 commit，让事件与调用方当前事务一起提交
        （唯一约束 ``(run_id, sequence)`` 兜底，冲突时会显式报错而不是静默丢事件）。
        """
        sequence = await self.session.scalar(
            select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
        )
        event = RunEvent(
            run_id=run_id,
            sequence=(sequence or 0) + 1,
            node=node,
            event_type=event_type,
            status=status,
            tool_name=tool_name,
            input_summary=input_summary or {},
            observation_summary=observation_summary or {},
            decision_reason=decision_reason,
            budget_summary=budget_summary or {},
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_events(self, run_id: str, limit: int, offset: int):
        """分页读取某运行的事件流，按序号升序。

        实现：条件过滤 run_id 后按 ``sequence`` 排序，再 limit/offset；
        另做一次 count 提供总数，供分页响应使用。
        """
        items = (
            await self.session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.sequence)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        total = await self.session.scalar(
            select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)
        )
        return list(items), total or 0

    async def list_budget(self, run_id: str) -> list[BudgetLedger]:
        """按写入顺序列出某运行的全部预算账本条目。

        实现：按 ``(created_at, id)`` 排序保证同一时刻写入的条目顺序稳定；
        账本是 append-only 的，这里不做任何聚合，聚合交给 ``budget_summary``。
        """
        return list(
            (
                await self.session.scalars(
                    select(BudgetLedger)
                    .where(BudgetLedger.run_id == run_id)
                    .order_by(BudgetLedger.created_at, BudgetLedger.id)
                )
            ).all()
        )

    async def list_attempts(self, run_id: str) -> list[DispatchAttempt]:
        """按动作序号列出某运行的全部派发尝试。

        实现：条件过滤后按 ``sequence`` 升序，与动作执行顺序一致；
        这些记录用于还原“哪一步开始过、哪一步被跳过或中断”。
        """
        return list(
            (
                await self.session.scalars(
                    select(DispatchAttempt)
                    .where(DispatchAttempt.run_id == run_id)
                    .order_by(DispatchAttempt.sequence)
                )
            ).all()
        )

    async def budget_summary(self, run_id: str, resource: str) -> dict[str, int]:
        """按状态汇总某类资源的预算用量。

        作用：把 append-only 的账本折叠成“已预留/已消耗/已释放”三个数字，
        供“是否还允许再消耗一次”判断使用。
        实现：先扫一遍找出已消费条目的 ``related_id`` 集合（某次预留已结算），
        再累加时跳过这些已结算的 ``reserved`` 条目——
        这样账本保持只追加，但对外的“未结算预留”不会重复计数。
        """
        rows = await self.list_budget(run_id)
        result = {"reserved": 0, "consumed": 0, "released": 0}
        settled = {
            row.related_id for row in rows if row.resource == resource and row.status == "consumed"
        }
        for row in rows:
            if row.resource != resource:
                continue
            # A reservation settles into consumption: the ledger stays append-only,
            # but the reserved amount is no longer outstanding once it is consumed.
            if row.status == "reserved" and row.related_id in settled:
                continue
            result[row.status] += row.amount
        return result

    async def begin_attempt(self, run: Run, action: dict, limit: int) -> DispatchAttempt | None:
        """为下一个动作开一次派发尝试，并先预留一次预算。

        作用：把“预算检查 + 预留 + 记事件”放在同一事务里，
        避免检查与预留之间被其他路径插队。
        实现：先以状态和动作游标为条件取得数据库写锁，取消先提交时拒绝派发；
        写锁保持到预算与尝试一起提交。用 ``budget_summary`` 算出已用量
        （consumed + reserved - released），达到 ``limit`` 直接返回 None 表示预算耗尽；
        否则插入 ``dispatching`` 状态的尝试行（序号 = current_action + 1）、
        追加一条 ``reserved`` 账本（``related_id`` 指向该尝试），
        再记一条 ``started`` 事件，最后统一 commit。
        """
        eligible = await self.session.execute(
            update(Run)
            .where(
                Run.id == run.id,
                Run.status == RUNNING,
                Run.current_action == run.current_action,
            )
            .values(status=RUNNING)
            .execution_options(synchronize_session=False)
        )
        if eligible.rowcount != 1:
            await self.session.commit()
            await self.session.refresh(run)
            return None
        budget = await self.budget_summary(run.id, "simulation_action")
        used = budget["consumed"] + budget["reserved"] - budget["released"]
        if used >= limit:
            await self.session.commit()
            return None
        attempt = DispatchAttempt(
            run_id=run.id,
            sequence=run.current_action + 1,
            status="dispatching",
            started_at=utc_now(),
        )
        self.session.add(attempt)
        await self.session.flush()
        self.session.add(
            BudgetLedger(
                run_id=run.id,
                resource="simulation_action",
                action="reserve",
                amount=1,
                status="reserved",
                related_id=attempt.id,
            )
        )
        await self.session.flush()
        await self.add_event(
            run.id,
            node="execute",
            event_type="started",
            status="running",
            tool_name="simulation_action",
            input_summary={"name": action["name"], "sequence": attempt.sequence},
            budget_summary={"resource": "simulation_action", "reserved": 1},
        )
        await self.session.commit()
        return attempt

    async def finish_attempt(self, attempt: DispatchAttempt, outcome: str) -> None:
        """结算一次派发尝试，并把预留转换成消耗。

        作用：动作结束后必须留下“实际用掉一次”的证据，同时让预留不再挂账。
        实现：按 ``outcome`` 把尝试置为 ``completed``/``failed`` 并写结束时间，
        失败时记录 ``simulated_failure`` 错误类型；
        再追加一条 ``consumed`` 账本（``related_id`` 指向同一尝试），
        这样 ``budget_summary`` 会把对应的 ``reserved`` 视为已结算。
        """
        attempt.status = "completed" if outcome == "completed" else "failed"
        attempt.ended_at = utc_now()
        attempt.error_kind = None if outcome == "completed" else "simulated_failure"
        self.session.add(
            BudgetLedger(
                run_id=attempt.run_id,
                resource="simulation_action",
                action="consume",
                amount=1,
                status="consumed",
                related_id=attempt.id,
            )
        )
        await self.session.commit()

    async def record_interrupted(self) -> int:
        """启动时的恢复：把上次进程留下的在途工作标记为未知，并把运行置为中断。

        作用：单进程设计不重放任何在途动作，因此重启后必须如实记录“结果未知”，
        而不是假装成功或自动重试（后者可能造成重复副作用）。
        实现：两趟扫描——
        ① 所有 ``dispatching`` 的尝试改成 ``unknown`` + ``process_interrupted``，
           并追加一条 ``consume_unknown`` 账本（仍算消耗，避免预算被“洗回来”）；
        ② 所有还在 ``running``/``cancelling`` 的运行改成 ``interrupted`` 终态，
           写 stop_reason、ended_at、workflow_error，并各记一条 ``interrupted`` 事件。
        返回被中断的运行数量，便于启动日志报告。
        """
        attempts = (
            await self.session.scalars(
                select(DispatchAttempt).where(DispatchAttempt.status == "dispatching")
            )
        ).all()
        for attempt in attempts:
            attempt.status = "unknown"
            attempt.ended_at = utc_now()
            attempt.error_kind = "process_interrupted"
            self.session.add(
                BudgetLedger(
                    run_id=attempt.run_id,
                    resource="simulation_action",
                    action="consume_unknown",
                    amount=1,
                    status="consumed",
                    related_id=attempt.id,
                )
            )
        runs = (
            await self.session.scalars(select(Run).where(Run.status.in_([RUNNING, CANCELLING])))
        ).all()
        for run in runs:
            run.status = "interrupted"
            run.stop_reason = "interrupted"
            run.ended_at = utc_now()
            run.workflow_error = "Process interrupted before the run reached a terminal state"
            await self.add_event(
                run.id,
                node="execute",
                event_type="interrupted",
                status="interrupted",
                decision_reason="Service restarted; no in-flight action was replayed",
            )
        await self.session.commit()
        return len(runs)

    async def release_unstarted_reservations(self) -> None:
        """把从未真正派发的预留释放掉。

        作用：若一次尝试只被创建为 ``pending`` 就随进程退出，它并没有产生任何副作用，
        这部分预算应当归还，否则重启后会凭空少一次额度。
        实现：查出所有 ``pending`` 尝试，置为 ``skipped`` 并标注 ``not_dispatched``，
        同时追加一条 ``released`` 账本；``budget_summary`` 计算已用量时会减去 released。
        """
        pending = (
            await self.session.scalars(
                select(DispatchAttempt).where(DispatchAttempt.status == "pending")
            )
        ).all()
        for attempt in pending:
            attempt.status = "skipped"
            attempt.error_kind = "not_dispatched"
            self.session.add(
                BudgetLedger(
                    run_id=attempt.run_id,
                    resource="simulation_action",
                    action="release",
                    amount=1,
                    status="released",
                    related_id=attempt.id,
                )
            )
        await self.session.commit()
