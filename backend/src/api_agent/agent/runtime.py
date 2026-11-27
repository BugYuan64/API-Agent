"""执行有预算和总时限的生成、执行、诊断及补测闭环。

依赖 AgentService 调用模型与执行层、AgentStore 保存记录；发送前持久化预留，
执行事实与模型解释分别记录，不自行发起目标 HTTP 请求。
"""

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from api_agent.agent.prompts import VERSION
from api_agent.agent.schemas import (
    AdaptRequest,
    AgentRunRequest,
    DiagnoseRequest,
    GenerateRequest,
)
from api_agent.agent.service import AgentError, AgentService, CallBudget, evidence_for
from api_agent.agent.store import AgentStore
from api_agent.execution.schemas import RunResult, empty_result, origin_allowed


def now():
    """返回带 UTC 时区的 ISO 时间文本，用于持久化事件而非计算经过时长。"""
    return datetime.now(UTC).isoformat()


class AgentRuntime:
    """编排单次后台运行，将状态变化、保守预算结算和降级结果写入存储。"""

    def __init__(self, agent: AgentService, store: AgentStore):
        """绑定本次运行的模型/执行服务与产物存储，不创建后台任务。"""
        self.agent = agent
        self.store = store

    def create(self, request: AgentRunRequest) -> dict:
        # Creation validates scope/engine before accepting a background task.
        """验证引擎和目标后冻结脱敏请求，创建并保存 queued 运行记录。

        不发送模型或目标请求；范围、引擎及持久化错误直接上抛给接收入口。
        """
        self.agent.execution.resolve_engine(request.engine)
        if not origin_allowed(request.scope.origin, self.agent.settings.target_allowed_origins):
            raise AgentError("origin_not_allowed")
        self.agent.context.learn(request.model_dump())
        snapshot = self.agent.context.clean(request.model_dump())
        snapshot["contract"] = self.agent.context.contract(request.contract)
        snapshot["generator_version"] = VERSION
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True).encode()
        record = {
            "schema_version": "1",
            "id": str(uuid.uuid4()),
            "created_at": now(),
            "status": "queued",
            "report_status": "partial",
            "stop_reason": None,
            "snapshot": snapshot,
            "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
            "actual_llm_status": "not_called",
            "degradation_reasons": [],
            "rounds": [],
            "events": [],
            "model_calls": [],
            "commentary": None,
            "budget": {
                "target_http": {
                    "limit": request.limits.target_requests,
                    "reserved": 0,
                    "consumed": 0,
                    "released": 0,
                    "observed_attempts": 0,
                    "unknown_requests": 0,
                },
                "model_call": {
                    "limit": request.limits.model_calls,
                    "reserved": 0,
                    "consumed": 0,
                    "released": 0,
                },
            },
        }
        self.event(record, "plan", "queued", "冻结脱敏输入，不自动重放历史运行。")
        return record

    def save(self, record):
        # The initial snapshot was scrubbed before hashing. Later discoveries must
        # never rewrite it; only newly appended evidence is scrubbed with new secrets.
        """清洗可追加字段并保存记录，保持初始快照及其哈希不随新发现改写。

        存储失败直接上抛，避免把未持久化状态描述为已落盘。
        """
        safe = self.agent.context.clean({k: v for k, v in record.items() if k != "snapshot"})
        safe["snapshot"] = record["snapshot"]
        self.store.save(safe)

    def event(self, record, node, status, reason=""):
        """追加带顺序号、UTC 时间与脱敏原因的事件，并立即持久化整条记录。"""
        record["events"].append(
            {
                "sequence": len(record["events"]) + 1,
                "timestamp": now(),
                "node": node,
                "status": status,
                "reason": self.agent.context.text(reason),
            }
        )
        self.save(record)

    def degrade(self, record, code):
        """去重记录退化原因，将模型与报告标为退化/部分完成并保存事件。"""
        if code not in record["degradation_reasons"]:
            record["degradation_reasons"].append(code)
        record["actual_llm_status"] = "degraded"
        record["report_status"] = "partial"
        self.event(record, "analyze", "degraded", code)

    async def run(self, record: dict, request: AgentRunRequest):
        """在总时限内执行闭环并尝试生成概述，最终保存状态、时长和报告事件。

        超时与业务错误写入停止原因；取消写入状态后继续传播 CancelledError，
        其他运行异常转换为安全错误码，最终持久化失败仍可向任务管理器传播。
        """
        budget = CallBudget(request.limits.model_calls)

        async def update():
            """将共享模型预算及调用轨迹同步到记录并落盘，供预留与结算回调使用。"""
            record["model_calls"] = [asdict(call) for call in budget.calls]
            record["budget"]["model_call"].update(
                reserved=budget.reserved,
                consumed=budget.consumed,
                prompt_tokens=budget.prompt_tokens,
                completion_tokens=sum(call.completion_tokens for call in budget.calls),
            )
            self.save(record)

        budget.changed = update
        started = time.monotonic()
        record["status"] = "running"
        record["started_at"] = now()
        self.event(record, "generate", "running")
        try:
            async with asyncio.timeout(request.limits.wall_time_seconds):
                await self.loop(record, request, budget)
                if budget.remaining and record["stop_reason"] != "model_budget_exhausted":
                    try:
                        # Entire response payloads are excluded; only normalized evidence travels.
                        commentary = await self.agent.report(
                            {
                                "snapshot": record["snapshot"],
                                "rounds": record["rounds"],
                                "events": record["events"],
                                "budget": record["budget"],
                                "stop_reason": record["stop_reason"],
                            },
                            budget,
                        )
                        record["commentary"] = self.agent.context.clean(commentary.model_dump())
                    except AgentError as error:
                        self.degrade(record, str(error))
                record["status"] = "completed"
                if not record["degradation_reasons"]:
                    record["report_status"] = "complete"
        except TimeoutError:
            record.update(status="timed_out", stop_reason="wall_time_exhausted")
        except asyncio.CancelledError:
            record.update(status="cancelled", stop_reason="cancelled")
            raise
        except AgentError as error:
            record.update(status="error", stop_reason=str(error))
            self.degrade(record, str(error))
        except Exception:
            # Engine/provider internals may contain secrets. Preserve facts without their text.
            record.update(status="error", stop_reason="agent_internal_error")
            self.degrade(record, "agent_internal_error")
        finally:
            record["ended_at"] = now()
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            if record["status"] != "completed":
                record["report_status"] = "partial"
            self.event(record, "report", record["report_status"], record["stop_reason"] or "")

    async def loop(self, record, request, budget):
        """生成首轮计划，逐轮执行、形成证据、诊断并筛选补测，直至停止条件。

        派发前预留目标预算，中断时按未知结果保守结算；每轮仅向后续模型
        传递规范证据，保留取消信号，执行或校验异常由外层 run 处理。
        """
        initial = await self.agent.generate(
            GenerateRequest(
                contract=request.contract,
                scope=request.scope,
                max_cases=min(request.max_cases, request.limits.target_requests),
            ),
            budget,
        )
        record["actual_llm_status"] = "used"
        plan = initial.plan
        previous = []
        signatures = set()
        stagnant = 0
        target = record["budget"]["target_http"]
        for index in range(request.limits.max_rounds):
            round_ = {
                "index": index,
                "plan": self.agent.context.clean(plan.model_dump()),
                "intents": [i.model_dump() for i in initial.intents] if index == 0 else [],
                "notes": initial.notes if index == 0 else ["自适应期望由模型建议，需核对契约。"],
                "result": None,
                "evidence": [],
                "diagnosis": None,
                "adaptation": None,
            }
            record["rounds"].append(round_)
            amount = len(plan.requests)
            if amount > target["limit"] - target["consumed"] - target["reserved"]:
                record["stop_reason"] = "target_budget_exhausted"
                break
            target["reserved"] += amount
            self.event(record, "execute", "dispatching", f"第 {index} 轮预留 {amount} 个请求。")
            result = None
            try:
                raw = await self.agent.execution.run(plan.model_dump(), request.engine)
                if "executions" not in raw:
                    result = empty_result(plan, "error", raw.get("error_kind", "execution_error"))
                else:
                    result = RunResult.model_validate(raw)
            finally:
                target["reserved"] -= amount
                target["consumed"] += amount
                if result is None:
                    target["unknown_requests"] += amount
                    round_["result"] = empty_result(
                        plan, "cancelled", "dispatch_interrupted"
                    ).model_dump(mode="json")
                    round_["evidence"] = [
                        item.model_dump()
                        for item in evidence_for(
                            DiagnoseRequest(
                                contract=request.contract,
                                plan=plan,
                                result=RunResult.model_validate(round_["result"]),
                                round_index=index,
                            ),
                            self.agent.context,
                        )
                    ]
                self.save(record)
            target["observed_attempts"] += result.observed_attempts
            target["unknown_requests"] += result.unknown_requests
            safe_result = result.model_dump(mode="json")
            for case in safe_result["executions"]:
                case["response"] = None  # No response body/header exfiltration to model/artifacts.
            round_["result"] = self.agent.context.clean(safe_result)
            diagnosis_request = DiagnoseRequest(
                contract=request.contract,
                plan=plan,
                result=result,
                round_index=index,
            )
            facts = evidence_for(diagnosis_request, self.agent.context)
            round_["evidence"] = [fact.model_dump() for fact in facts]
            self.event(record, "assert", result.status, f"第 {index} 轮，真实执行结果已保存。")
            # Some drivers normalize CancelledError into RunResult. Reassert the outer
            # task's cancellation so asyncio.timeout / explicit cancel remain authoritative.
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            if result.status == "cancelled":
                record["stop_reason"] = "execution_cancelled"
                break
            current = {(f.kind, f.passed, f.http_status, f.error_kind) for f in facts}
            stagnant = stagnant + 1 if current <= signatures else 0
            signatures.update(current)
            if budget.remaining <= 0:
                record["stop_reason"] = "model_budget_exhausted"
                self.degrade(record, "model_budget_exhausted")
                break
            try:
                diagnosis = await self.agent.diagnose(diagnosis_request, budget)
                round_["diagnosis"] = self.agent.context.clean(diagnosis.model_dump())
                self.event(record, "analyze", "completed", f"第 {index} 轮假设引用校验通过。")
            except AgentError as error:
                self.degrade(record, str(error))
                record["stop_reason"] = str(error)
                break
            remaining = target["limit"] - target["consumed"]
            if not remaining:
                record["stop_reason"] = "target_budget_exhausted"
                break
            if index + 1 >= request.limits.max_rounds:
                record["stop_reason"] = "max_rounds_reached"
                break
            if stagnant >= 2:
                record["stop_reason"] = "no_progress"
                break
            if not diagnosis.analysis.hypotheses:
                record["stop_reason"] = "no_hypotheses"
                break
            try:
                decision = await self.agent.adapt(
                    AdaptRequest(
                        contract=request.contract,
                        plan=plan,
                        diagnosis=diagnosis,
                        previous_requests=previous,
                        remaining_requests=min(remaining, 50),
                        allow_mutating_followups=request.allow_mutating_followups,
                        round_index=index + 1,
                    ),
                    budget,
                )
                round_["adaptation"] = self.agent.context.clean(decision.model_dump())
                self.event(record, "strategize", "completed", decision.reason)
            except AgentError as error:
                record["stop_reason"] = str(error)
                self.degrade(record, str(error))
                break
            if not decision.continue_testing or decision.plan is None:
                record["stop_reason"] = "no_valid_candidates"
                break
            previous.extend(plan.requests)
            plan = decision.plan
