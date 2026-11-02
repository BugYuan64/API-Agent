"""Run creation and read-only lifecycle queries."""

from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import not_found
from api_agent.infrastructure.orm import new_id
from api_agent.projects.models import Project
from api_agent.runs.models import Run, RunSnapshot
from api_agent.runs.repository import RunRepository
from api_agent.runs.schemas import RunCreate, RunResponse, RunSnapshotResponse


class RunService:
    """运行创建与只读查询的业务层。

    作用：把“创建时先校验项目存在”“快照与运行记录成对写入”“首条事件先落库”
    这些跨表约定集中在此；运行推进由 worker 负责，本类不执行任何动作。
    实现：构造时接收请求级 session 与配置中的 ``llm_mode``，
    后者只是如实记录到运行记录上，模拟模式不会真的调用模型。
    """

    def __init__(self, session: AsyncSession, llm_mode: str = "disabled"):
        """保存会话、创建仓储并记录本次部署的模型模式。

        实现：``llm_mode`` 会被原样写进运行记录，用于事后证明
        “这次运行配置的是什么模式、实际有没有用到模型”。
        """
        self.session = session
        self.repository = RunRepository(session)
        self.llm_mode = llm_mode

    async def create(self, data: RunCreate) -> RunResponse:
        """创建一次模拟运行：写入快照 + 运行记录 + 首条 created 事件。

        实现：先校验项目存在（否则 404）；把请求里的动作列表和资源上限
        ``model_dump`` 成纯 JSON 存入快照（固定 run_mode=simulation）；
        手动给快照预赋 ID，使运行记录能直接引用它并同事务写入；
        再补一条 ``validate/created`` 事件记录动作数与模式，
        让事件流从第一秒起就有可审计的起点；最后提交并返回运行详情。
        运行此时是 ``queued``，等待 worker 认领。
        """
        if await self.session.get(Project, data.project_id) is None:
            raise not_found("Project")
        snapshot = RunSnapshot(
            project_id=data.project_id,
            run_mode="simulation",
            actions=[action.model_dump() for action in data.actions],
            resource_limits=data.resource_limits.model_dump(),
            random_seed=data.random_seed,
        )
        snapshot.id = new_id()
        run = Run(
            snapshot_id=snapshot.id,
            run_mode="simulation",
            configured_llm_mode=self.llm_mode,
            actual_llm_status="not_applicable",
            action_count=len(data.actions),
        )
        await self.repository.add(snapshot, run)
        await self.repository.add_event(
            run.id,
            node="validate",
            event_type="created",
            status="queued",
            input_summary={"action_count": len(data.actions), "mode": "simulation"},
            decision_reason="Validated local simulation snapshot; no target or model request",
        )
        await self.session.commit()
        return RunResponse.model_validate(run)

    async def get(self, run_id: str) -> RunResponse:
        """按 ID 查询运行详情。

        实现：交给仓储 ``get``（会一并加载 snapshot），判空后抛 404；
        其余字段由 ``RunResponse`` 的白名单定义对外可见范围。
        """
        run = await self.repository.get(run_id)
        if run is None:
            raise not_found("Run")
        return RunResponse.model_validate(run)

    async def get_snapshot(self, run_id: str) -> RunSnapshotResponse:
        """查询某运行创建时冻结的动作快照。

        作用：快照是不可变证据，用它可以在不依赖当前配置的情况下复现这次运行。
        实现：先按 run_id 找到运行（判空 404），再用 ``snapshot_id`` 取快照行并转换。
        """
        run = await self.repository.get(run_id)
        if run is None:
            raise not_found("Run")
        snapshot = await self.repository.get_snapshot(run.snapshot_id)
        return RunSnapshotResponse.model_validate(snapshot)

    async def cancel(self, run_id: str) -> RunResponse:
        """请求取消一次运行。

        实现：委托仓储 ``request_cancel`` 做状态分支处理
        （排队中直接取消、运行中转为 cancelling、重复请求幂等）；
        真正的收尾由 worker 在下一个安全点完成。
        """
        return RunResponse.model_validate(await self.repository.request_cancel(run_id))
