"""模型测试后台执行管理器、凭证解析器与历史持久化仓储。

职责边界：
- ``resolve_testing_credentials`` 脱离 FastAPI Request/ApiError，统一执行 Base URL 规范化
  与同源已保存密钥复用策略；
- ``ModelTestRepository`` 负责 ``model_test_runs`` 表的读写、脱敏清洗与重启中断恢复，
  绝不落库任何明文 ``api_key``；
- ``ModelTestManager`` 参照 ``TaskManager`` / ``StreamManager`` 生命周期管理手动与 AI 多轮
  后台模型探测任务，支持轮间可中断冷却、可重连 NDJSON 实时流、主动取消与优雅停机回收。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.history import sanitize_model_test_run
from api_agent.infrastructure.orm import as_utc, new_id, utc_now
from api_agent.infrastructure.privacy import Context
from api_agent.llm import testing as llm_testing
from api_agent.llm.models import ModelTestRunRecord
from api_agent.llm.service import LLMSettingService, same_origin
from api_agent.llm.testing import (
    build_initial_profile,
    build_probe_observation,
    build_protocol_headers,
    detect_protocol,
    normalize_base_url,
    probe_single_model,
)
from api_agent.llm.testing_schemas import (
    ModelProfile,
    ModelProtocol,
    ModelTestAiSuiteSpec,
    ModelTestRunStartRequest,
    ModelTestRunSummary,
    ModelTestSubCaseSnapshot,
    ProbeCheckName,
    ResolvedModelProtocol,
)

logger = logging.getLogger("api_agent")

DEFAULT_AI_BADGE = "AI 测试"
COOLDOWN_SLICE_SECONDS = 0.1
MAX_ACTIVE_RUNS = 4

_UNSET: Any = object()

__all__ = [
    "DEFAULT_AI_BADGE",
    "LiveModelTestRun",
    "ModelTestAiSuiteSpec",
    "ModelTestManager",
    "ModelTestRepository",
    "ModelTestRunActiveError",
    "ModelTestingConfigError",
    "build_case_link_block",
    "resolve_testing_credentials",
    "status_tone",
]


class ModelTestingConfigError(ValueError):
    """模型测试参数、目标地址或并发容量不满足运行条件时抛出的领域错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ModelTestRunActiveError(RuntimeError):
    """尝试删除仍在排队或运行中的模型测试任务时抛出。"""


async def resolve_testing_credentials(
    session: AsyncSession,
    settings: Settings,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    use_saved_key: bool = False,
) -> tuple[str, str, bool, bool]:
    """解析模型测试服务的目标地址与临时密钥，严格阻止跨源复用已保存密钥。

    返回 ``(resolved_base_url, resolved_api_key, used_saved_key, has_custom_api_key)``；
    无可用密钥时 ``resolved_api_key`` 为空字符串 ``""``。
    """
    service = LLMSettingService(session, settings)
    saved = await service.get_client()

    raw_url = base_url.strip() if isinstance(base_url, str) else ""
    target_url = normalize_base_url(raw_url) if raw_url else normalize_base_url(saved.base_url)
    if not target_url:
        raise ModelTestingConfigError("invalid_base_url", "请填写合法的 http(s) Base URL")

    custom_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else ""
    if custom_key:
        return target_url, custom_key, False, True

    saved_key = (
        saved.api_key.strip() if isinstance(saved.api_key, str) and saved.api_key.strip() else ""
    )
    if use_saved_key and saved_key and same_origin(target_url, saved.base_url):
        return target_url, saved_key, True, False

    return target_url, "", False, False


def _iso_utc(value: datetime | None) -> str | None:
    normalized = as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def status_tone(status: str) -> str:
    """将模型子用例状态映射为统一的展示色调标签。"""
    if status in {"available", "supported"}:
        return "success"
    if status in {"partial", "inferred", "unsupported"}:
        return "warning"
    if status in {"unavailable", "error"}:
        return "danger"
    return "neutral"


def build_case_link_block(
    *,
    run_id: str,
    case_id: str,
    round_index: int,
    label: str,
    status: str,
) -> dict[str, Any]:
    """构造与全站统一契约一致的模型测试子用例回放链接块。"""
    return {
        "type": "case_link",
        "scope": "model_testing",
        "history_id": run_id,
        "case_id": case_id,
        "round": round_index,
        "label": label,
        "status": status,
        "tone": status_tone(status),
    }


def _secret_context(extra_secrets: tuple[str, ...] = ()) -> Context:
    valid = tuple(
        sorted(
            {s for s in extra_secrets if isinstance(s, str) and len(s) >= 4},
            key=len,
            reverse=True,
        )
    )
    return Context(secrets=valid)


def _scrub_record(record: ModelTestRunRecord, extra_secrets: tuple[str, ...] = ()) -> None:
    """落库前使用临时秘密上下文 + 共享 ``sanitize_model_test_run`` 双重清洗记录。"""
    if extra_secrets:
        ctx = _secret_context(extra_secrets)
        if ctx.redactor.secrets:
            if record.title:
                record.title = ctx.text(record.title)
            if record.base_url:
                record.base_url = ctx.text(record.base_url)
            if record.error_message:
                record.error_message = ctx.text(record.error_message)
            if record.summary_text:
                record.summary_text = ctx.text(record.summary_text)
            if record.discover_meta is not None:
                record.discover_meta = ctx.clean(record.discover_meta)
            if record.models_snapshot is not None:
                record.models_snapshot = ctx.clean(record.models_snapshot)
            if record.rounds_json is not None:
                record.rounds_json = ctx.clean(record.rounds_json)
            if record.sub_cases_json is not None:
                record.sub_cases_json = ctx.clean(record.sub_cases_json)
            if record.summary_json is not None:
                record.summary_json = ctx.clean(record.summary_json)
    sanitize_model_test_run(record)


def _serialize_run_list_item(record: ModelTestRunRecord) -> dict[str, Any]:
    sub_cases = record.sub_cases_json if isinstance(record.sub_cases_json, list) else []
    return {
        "id": record.id,
        "title": record.title,
        "source": record.source,
        "badge": record.badge,
        "status": record.status,
        "error_message": record.error_message,
        "base_url": record.base_url,
        "protocol": record.protocol,
        "resolved_protocol": record.resolved_protocol,
        "used_saved_key": bool(record.used_saved_key),
        "has_custom_api_key": bool(record.has_custom_api_key),
        "timeout_seconds": float(record.timeout_seconds),
        "concurrency": int(record.concurrency),
        "checks": list(record.checks or []),
        "target_models": list(record.target_models or []),
        "cooldown_ms": int(record.cooldown_ms),
        "total_rounds": int(record.total_rounds),
        "completed_rounds": int(record.completed_rounds),
        "sub_case_count": len(sub_cases),
        "summary": copy.deepcopy(record.summary_json)
        if isinstance(record.summary_json, dict)
        else None,
        "summary_text": record.summary_text,
        "started_at": _iso_utc(record.started_at),
        "finished_at": _iso_utc(record.finished_at),
        "created_at": _iso_utc(record.created_at),
        "updated_at": _iso_utc(record.updated_at),
    }


def _serialize_run_detail(record: ModelTestRunRecord) -> dict[str, Any]:
    item = _serialize_run_list_item(record)
    item["discover_meta"] = (
        copy.deepcopy(record.discover_meta) if isinstance(record.discover_meta, dict) else None
    )
    item["models_snapshot"] = (
        copy.deepcopy(record.models_snapshot) if isinstance(record.models_snapshot, list) else []
    )
    item["rounds"] = (
        copy.deepcopy(record.rounds_json) if isinstance(record.rounds_json, list) else []
    )
    item["sub_cases"] = (
        copy.deepcopy(record.sub_cases_json) if isinstance(record.sub_cases_json, list) else []
    )
    return item


class ModelTestRepository:
    """``model_test_runs`` 表的异步持久化仓储，统一执行写前/读时脱敏，绝不存储明文密钥。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_run(
        self,
        *,
        base_url: str,
        protocol: ModelProtocol,
        resolved_protocol: ResolvedModelProtocol | None,
        used_saved_key: bool,
        has_custom_api_key: bool,
        timeout_seconds: float,
        concurrency: int,
        checks: list[str],
        target_models: list[str],
        cooldown_ms: int = 500,
        total_rounds: int = 1,
        title: str | None = None,
        source: str = "manual",
        badge: str | None = None,
        status: str = "running",
        discover_meta: dict[str, Any] | None = None,
        models_snapshot: list[dict[str, Any]] | None = None,
        started_at: datetime | None = None,
        run_id: str | None = None,
        extra_secrets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """创建并提交一条新的模型探测历史记录，返回脱敏详情字典。"""
        now = utc_now()
        record = ModelTestRunRecord(
            id=run_id or new_id(),
            title=title[:200] if isinstance(title, str) and title.strip() else None,
            source=source,
            badge=badge[:40] if isinstance(badge, str) and badge.strip() else None,
            status=status,
            base_url=base_url,
            protocol=protocol,
            resolved_protocol=resolved_protocol,
            used_saved_key=bool(used_saved_key),
            has_custom_api_key=bool(has_custom_api_key),
            timeout_seconds=float(timeout_seconds),
            concurrency=int(concurrency),
            checks=list(checks),
            target_models=list(target_models),
            cooldown_ms=int(cooldown_ms),
            total_rounds=int(total_rounds),
            completed_rounds=0,
            discover_meta=copy.deepcopy(discover_meta) if discover_meta is not None else None,
            models_snapshot=copy.deepcopy(models_snapshot) if models_snapshot is not None else [],
            rounds_json=[],
            sub_cases_json=[],
            summary_json=None,
            summary_text=None,
            started_at=started_at or now,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        _scrub_record(record, extra_secrets)
        async with self.database.session() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            sanitize_model_test_run(record)
            return _serialize_run_detail(record)

    async def update_progress(
        self,
        run_id: str,
        *,
        status: Any = _UNSET,
        completed_rounds: Any = _UNSET,
        models_snapshot: Any = _UNSET,
        rounds_json: Any = _UNSET,
        sub_cases_json: Any = _UNSET,
        summary_json: Any = _UNSET,
        summary_text: Any = _UNSET,
        error_message: Any = _UNSET,
        discover_meta: Any = _UNSET,
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        """增量更新进行中任务的快照、轮次与子用例列表，提交前统一脱敏。"""
        async with self.database.session() as session:
            record = await session.get(ModelTestRunRecord, run_id)
            if record is None:
                return
            if status is not _UNSET:
                record.status = status
            if completed_rounds is not _UNSET:
                record.completed_rounds = int(completed_rounds)
            if models_snapshot is not _UNSET:
                record.models_snapshot = copy.deepcopy(models_snapshot)
            if rounds_json is not _UNSET:
                record.rounds_json = copy.deepcopy(rounds_json)
            if sub_cases_json is not _UNSET:
                record.sub_cases_json = copy.deepcopy(sub_cases_json)
            if summary_json is not _UNSET:
                record.summary_json = copy.deepcopy(summary_json)
            if summary_text is not _UNSET:
                record.summary_text = summary_text
            if error_message is not _UNSET:
                record.error_message = error_message
            if discover_meta is not _UNSET:
                record.discover_meta = copy.deepcopy(discover_meta)
            record.updated_at = utc_now()
            _scrub_record(record, extra_secrets)
            await session.commit()

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_rounds: Any = _UNSET,
        models_snapshot: Any = _UNSET,
        rounds_json: Any = _UNSET,
        sub_cases_json: Any = _UNSET,
        summary_json: Any = _UNSET,
        summary_text: Any = _UNSET,
        error_message: Any = _UNSET,
        extra_secrets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """写入任务终态与最终汇总快照，提交后返回脱敏详情字典。"""
        async with self.database.session() as session:
            record = await session.get(ModelTestRunRecord, run_id)
            if record is None:
                raise LookupError("model_test_run_not_found")
            record.status = status
            if completed_rounds is not _UNSET:
                record.completed_rounds = int(completed_rounds)
            if models_snapshot is not _UNSET:
                record.models_snapshot = copy.deepcopy(models_snapshot)
            if rounds_json is not _UNSET:
                record.rounds_json = copy.deepcopy(rounds_json)
            if sub_cases_json is not _UNSET:
                record.sub_cases_json = copy.deepcopy(sub_cases_json)
            if summary_json is not _UNSET:
                record.summary_json = copy.deepcopy(summary_json)
            if summary_text is not _UNSET:
                record.summary_text = summary_text
            if error_message is not _UNSET:
                record.error_message = error_message
            now = utc_now()
            record.finished_at = now
            record.updated_at = now
            _scrub_record(record, extra_secrets)
            await session.commit()
            await session.refresh(record)
            sanitize_model_test_run(record)
            return _serialize_run_detail(record)

    async def list_runs(self, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """按创建时间倒序分页返回脱敏后的模型探测历史列表与总数。"""
        bounded_limit = max(1, min(200, int(limit)))
        bounded_offset = max(0, int(offset))
        async with self.database.session() as session:
            rows = (
                await session.scalars(
                    select(ModelTestRunRecord)
                    .order_by(ModelTestRunRecord.created_at.desc(), ModelTestRunRecord.id.desc())
                    .limit(bounded_limit)
                    .offset(bounded_offset)
                )
            ).all()
            total = await session.scalar(select(func.count()).select_from(ModelTestRunRecord)) or 0
            items = []
            for row in rows:
                sanitize_model_test_run(row)
                items.append(_serialize_run_list_item(row))
            return items, int(total)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """读取单条模型探测任务的完整脱敏详情，不存在时返回 ``None``。"""
        async with self.database.session() as session:
            record = await session.get(ModelTestRunRecord, run_id)
            if record is None:
                return None
            sanitize_model_test_run(record)
            return _serialize_run_detail(record)

    async def get_sub_case(self, run_id: str, case_id: str) -> dict[str, Any] | None:
        """读取指定任务下单个子用例的只读回放快照，未命中时返回 ``None``。"""
        run = await self.get_run(run_id)
        if run is None:
            return None
        for sub_case in run.get("sub_cases") or []:
            if isinstance(sub_case, dict) and sub_case.get("case_id") == case_id:
                return copy.deepcopy(sub_case)
        return None

    async def delete_run(self, run_id: str) -> bool:
        """删除单条模型探测历史记录，返回是否命中删除。"""
        async with self.database.session() as session:
            result = await session.execute(
                delete(ModelTestRunRecord).where(ModelTestRunRecord.id == run_id)
            )
            await session.commit()
            return bool(result.rowcount)

    async def clear_terminal_runs(self, *, exclude_ids: set[str] | None = None) -> list[str]:
        """删除全部终态历史记录并返回已删除的任务 ID 列表，不触碰活跃任务。"""
        async with self.database.session() as session:
            query = select(ModelTestRunRecord.id).where(
                ModelTestRunRecord.status.notin_(["queued", "running"])
            )
            if exclude_ids:
                query = query.where(ModelTestRunRecord.id.notin_(list(exclude_ids)))
            ids = list((await session.scalars(query)).all())
            if not ids:
                return []
            await session.execute(delete(ModelTestRunRecord).where(ModelTestRunRecord.id.in_(ids)))
            await session.commit()
            return ids

    async def clear_runs(self, *, exclude_ids: set[str] | None = None) -> int:
        """清空终态模型探测历史记录并返回删除条数。"""
        deleted = await self.clear_terminal_runs(exclude_ids=exclude_ids)
        return len(deleted)

    async def recover(self) -> int:
        """启动恢复：将上一进程遗留的 ``queued`` / ``running`` 记录标记为 ``interrupted``。"""
        now = utc_now()
        async with self.database.session() as session:
            result = await session.execute(
                update(ModelTestRunRecord)
                .where(ModelTestRunRecord.status.in_(["queued", "running"]))
                .values(
                    status="interrupted",
                    error_message="服务重启导致模型探测任务中断",
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)


class LiveModelTestRun:
    """运行中模型探测任务的内存实时视图与唤醒信号（绝不持有任何明文密钥）。"""

    def __init__(self, initial_detail: dict[str, Any]) -> None:
        self.run_id: str = initial_detail["id"]
        self.record: dict[str, Any] = copy.deepcopy(initial_detail)
        self.stage: str = "executing"
        self.current_round: int = 0
        self.current_models: list[str] = []
        self.cooldown_remaining_ms: int | None = None
        target_count = len(initial_detail.get("target_models") or [])
        total_rounds = int(initial_detail.get("total_rounds") or 1)
        self.total_cases: int = max(0, target_count * total_rounds)
        self.changed: asyncio.Event = asyncio.Event()
        self.terminal: bool = initial_detail.get("status") not in {"queued", "running"}

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def snapshot_frame(self, *, sent_cases: int = 0) -> dict[str, Any]:
        """构造增量或全量回放帧（仅含脱敏快照与新增子用例）。"""
        sub_cases = self.record.get("sub_cases") or []
        return {
            "run_id": self.run_id,
            "title": self.record.get("title"),
            "source": self.record.get("source"),
            "badge": self.record.get("badge"),
            "status": self.record.get("status"),
            "stage": self.stage,
            "base_url": self.record.get("base_url"),
            "protocol": self.record.get("protocol"),
            "resolved_protocol": self.record.get("resolved_protocol"),
            "current_round": self.current_round,
            "total_rounds": int(self.record.get("total_rounds") or 1),
            "completed_rounds": int(self.record.get("completed_rounds") or 0),
            "cooldown_ms": int(self.record.get("cooldown_ms") or 0),
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "current_models": list(self.current_models),
            "completed_cases": len(sub_cases),
            "total_cases": self.total_cases,
            "models_snapshot": copy.deepcopy(self.record.get("models_snapshot") or []),
            "new_sub_cases": copy.deepcopy(sub_cases[sent_cases:]),
            "summary": copy.deepcopy(self.record.get("summary")),
            "summary_text": self.record.get("summary_text"),
            "error_message": self.record.get("error_message"),
        }

    def active_item(self) -> dict[str, Any]:
        """生成跨页切回重连所需的活跃任务摘要项。"""
        sub_cases = self.record.get("sub_cases") or []
        return {
            "id": self.run_id,
            "title": self.record.get("title"),
            "source": self.record.get("source"),
            "badge": self.record.get("badge"),
            "status": self.record.get("status"),
            "stage": self.stage,
            "base_url": self.record.get("base_url"),
            "protocol": self.record.get("protocol"),
            "resolved_protocol": self.record.get("resolved_protocol"),
            "target_models": list(self.record.get("target_models") or []),
            "checks": list(self.record.get("checks") or []),
            "current_round": self.current_round,
            "total_rounds": int(self.record.get("total_rounds") or 1),
            "completed_rounds": int(self.record.get("completed_rounds") or 0),
            "cooldown_ms": int(self.record.get("cooldown_ms") or 0),
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "current_models": list(self.current_models),
            "completed_cases": len(sub_cases),
            "total_cases": self.total_cases,
            "started_at": self.record.get("started_at"),
            "created_at": self.record.get("created_at"),
        }


def _default_run_title(
    base_url: str,
    models: list[str],
    *,
    source: str,
    rounds: int,
) -> str:
    host = urlsplit(base_url).hostname or base_url
    prefix = "AI 模型探测" if source == "ai" else "模型能力探测"
    if len(models) == 1:
        model_label = models[0]
    else:
        model_label = f"{models[0]} 等 {len(models)} 个模型"
    round_hint = f"（{rounds} 轮）" if rounds > 1 else ""
    return f"{prefix} · {model_label} @ {host}{round_hint}"[:200]


def _derive_sub_case_status(profile: ModelProfile, check_set: set[ProbeCheckName]) -> str:
    """根据本次实测项收敛单个子用例的稳定状态值。"""
    if "availability" in check_set or "output_modalities" in check_set:
        return profile.availability.status
    statuses: list[str] = []
    if "tool_calling" in check_set:
        statuses.append(profile.tool_calling.status)
    if "streaming" in check_set:
        statuses.append(profile.streaming.status)
    if "structured_output" in check_set:
        statuses.append(profile.structured_output.status)
    if statuses:
        if any(s == "error" for s in statuses):
            return "error"
        if all(s == "supported" for s in statuses):
            return "supported"
        if any(s in {"supported", "partial"} for s in statuses):
            return "partial"
        if any(s == "unsupported" for s in statuses):
            return "unsupported"
        return statuses[0]
    if "input_modalities" in check_set:
        for mod in profile.input_modalities:
            if mod.modality == "image" and mod.source == "probed":
                return mod.status
    return "unknown"


def _build_run_summary(
    *,
    target_models: list[str],
    latest_profiles: dict[str, ModelProfile],
    sub_cases: list[dict[str, Any]],
    check_set: set[ProbeCheckName],
    completed_rounds: int,
    total_rounds: int,
) -> tuple[dict[str, Any], str]:
    """基于最新模型画像与全部子用例快照聚合结构化统计和可读摘要。"""
    probed_models = [latest_profiles[mid] for mid in target_models if mid in latest_profiles]
    tested_ids = {sc.get("model_id") for sc in sub_cases if isinstance(sc, dict)}
    tested_profiles = [p for p in probed_models if p.id in tested_ids]

    available_count = sum(1 for p in tested_profiles if p.availability.status == "available")
    unavailable_count = sum(1 for p in tested_profiles if p.availability.status == "unavailable")
    error_count = sum(1 for p in tested_profiles if p.availability.status == "error")
    tool_supported_count = sum(1 for p in tested_profiles if p.tool_calling.status == "supported")
    streaming_supported_count = sum(1 for p in tested_profiles if p.streaming.status == "supported")
    structured_output_supported_count = sum(
        1 for p in tested_profiles if p.structured_output.status in {"supported", "partial"}
    )
    vision_supported_count = sum(
        1
        for p in tested_profiles
        if any(
            m.modality == "image" and m.status == "supported" and m.source == "probed"
            for m in p.input_modalities
        )
    )

    latencies = [
        float(sc["latency_ms"])
        for sc in sub_cases
        if isinstance(sc, dict) and isinstance(sc.get("latency_ms"), (int, float))
    ]
    ttfts = [
        float(sc["ttft_ms"])
        for sc in sub_cases
        if isinstance(sc, dict) and isinstance(sc.get("ttft_ms"), (int, float))
    ]
    avg_latency_ms = round(sum(latencies) / len(latencies), 2) if latencies else None
    avg_ttft_ms = round(sum(ttfts) / len(ttfts), 2) if ttfts else None

    total_cases = len(target_models) * total_rounds
    round_detail = (
        f"（{completed_rounds}/{total_rounds} 轮，{len(sub_cases)}/{total_cases} 条用例）"
        if total_rounds > 1
        else ""
    )
    parts = [f"共探测 {len(tested_profiles)}/{len(target_models)} 个模型{round_detail}"]
    if "availability" in check_set or "output_modalities" in check_set:
        parts.append(f"可用 {available_count} 个")
        if unavailable_count:
            parts.append(f"不可用 {unavailable_count} 个")
        if error_count:
            parts.append(f"异常 {error_count} 个")
    if "tool_calling" in check_set:
        parts.append(f"支持工具调用 {tool_supported_count} 个")
    if "streaming" in check_set:
        parts.append(f"流式可用 {streaming_supported_count} 个")
    if "structured_output" in check_set:
        parts.append(f"结构化输出可用 {structured_output_supported_count} 个")
    if "input_modalities" in check_set:
        parts.append(f"支持图像输入 {vision_supported_count} 个")
    if avg_latency_ms is not None:
        parts.append(f"平均耗时 {avg_latency_ms:.0f}ms")

    message = "，".join(parts)
    summary = ModelTestRunSummary(
        tested_count=len(tested_profiles),
        total_cases=total_cases,
        completed_cases=len(sub_cases),
        available_count=available_count,
        unavailable_count=unavailable_count,
        error_count=error_count,
        tool_supported_count=tool_supported_count,
        streaming_supported_count=streaming_supported_count,
        structured_output_supported_count=structured_output_supported_count,
        vision_supported_count=vision_supported_count,
        avg_latency_ms=avg_latency_ms,
        avg_ttft_ms=avg_ttft_ms,
        completed_rounds=completed_rounds,
        total_rounds=total_rounds,
        message=message,
    )
    return summary.model_dump(mode="json"), message


async def _await_task_done(task: asyncio.Task[Any]) -> None:
    """事件驱动等待后台任务结束；调用方被取消时绝不取消后台任务本身。"""
    done = asyncio.Event()
    task.add_done_callback(lambda _completed: done.set())
    if task.done():
        done.set()
    await done.wait()


async def _invoke_callback(callback: Callable[..., Any] | None, payload: Any) -> None:
    if callback is None:
        return
    try:
        outcome = callback(payload)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:  # noqa: BLE001 - 外部回调异常不得中断后台探测任务
        logger.warning("Model test callback failed", exc_info=True)


class ModelTestManager:
    """管理持久化、可重连、支持多轮冷却与 AI 套件的后台模型测试任务。"""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.repo = ModelTestRepository(database)
        self.active: dict[str, asyncio.Task[Any]] = {}
        self.live: dict[str, LiveModelTestRun] = {}
        self._cancel_reasons: dict[str, str] = {}

    async def start_manual_run(
        self,
        session: AsyncSession,
        payload: ModelTestRunStartRequest,
    ) -> dict[str, Any]:
        """启动持久化手动后台模型探测任务，立即返回初始任务详情。"""
        return await self._start_run(session, payload, on_case_done=None)

    async def start_ai_multi_round_run(
        self,
        session: AsyncSession,
        spec: ModelTestAiSuiteSpec,
        *,
        on_case_done: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """启动一条 AI 多轮模型探测任务（单条带 AI 标注的历史、轮间冷却与子用例回调）。"""
        payload = ModelTestRunStartRequest(
            base_url=spec.base_url,
            api_key=spec.api_key,
            use_saved_key=spec.use_saved_key,
            protocol=spec.protocol,
            models=spec.models,
            checks=spec.checks,
            timeout_seconds=spec.timeout_seconds,
            concurrency=spec.concurrency,
            cooldown_ms=spec.cooldown_ms,
            rounds=spec.rounds,
            title=spec.title,
            source="ai",
            badge=spec.badge or DEFAULT_AI_BADGE,
            discover_meta=spec.discover_meta,
            discovered_profiles=spec.discovered_profiles,
        )
        return await self._start_run(session, payload, on_case_done=on_case_done)

    async def _start_run(
        self,
        session: AsyncSession,
        payload: ModelTestRunStartRequest,
        *,
        on_case_done: Callable[[dict[str, Any]], Any] | None,
    ) -> dict[str, Any]:
        if len(self.active) >= MAX_ACTIVE_RUNS:
            raise ModelTestingConfigError(
                "model_test_capacity_exceeded",
                "当前并发执行的模型探测任务过多，请稍后再试",
            )

        (
            resolved_base_url,
            resolved_api_key,
            used_saved_key,
            has_custom_api_key,
        ) = await resolve_testing_credentials(
            session,
            self.settings,
            base_url=payload.base_url,
            api_key=payload.api_key,
            use_saved_key=payload.use_saved_key,
        )
        resolved_protocol = detect_protocol(resolved_base_url, payload.protocol)
        source = payload.source
        badge = (
            payload.badge
            if payload.badge is not None
            else (DEFAULT_AI_BADGE if source == "ai" else None)
        )
        title = payload.title or _default_run_title(
            resolved_base_url,
            payload.models,
            source=source,
            rounds=payload.rounds,
        )

        discovered_map: dict[str, ModelProfile] = {}
        for prof in payload.discovered_profiles or []:
            if prof.id:
                discovered_map[prof.id] = prof.model_copy(deep=True)

        initial_profiles: dict[str, ModelProfile] = {}
        for model_id in payload.models:
            if model_id in discovered_map:
                initial_profiles[model_id] = discovered_map[model_id].model_copy(deep=True)
            else:
                built = build_initial_profile(model_id, protocol=resolved_protocol)
                if built is not None:
                    initial_profiles[model_id] = built

        initial_snapshot = [
            initial_profiles[mid].model_dump(mode="json")
            for mid in payload.models
            if mid in initial_profiles
        ]
        extra_secrets = (resolved_api_key,) if resolved_api_key else ()
        initial_detail = await self.repo.create_run(
            base_url=resolved_base_url,
            protocol=payload.protocol,
            resolved_protocol=resolved_protocol,
            used_saved_key=used_saved_key,
            has_custom_api_key=has_custom_api_key,
            timeout_seconds=payload.timeout_seconds,
            concurrency=payload.concurrency,
            checks=list(payload.checks),
            target_models=list(payload.models),
            cooldown_ms=payload.cooldown_ms,
            total_rounds=payload.rounds,
            title=title,
            source=source,
            badge=badge,
            status="running",
            discover_meta=payload.discover_meta,
            models_snapshot=initial_snapshot,
            started_at=utc_now(),
            extra_secrets=extra_secrets,
        )

        run_id = initial_detail["id"]
        live = LiveModelTestRun(initial_detail)
        self.live[run_id] = live

        # 仅在后台协程内部通过局部容器持有临时密钥，绝不挂到 self / live / DB。
        ephemeral_key_box = [resolved_api_key]
        resolved_api_key = ""

        task = asyncio.create_task(
            self._run_worker(
                run_id=run_id,
                live=live,
                base_url=resolved_base_url,
                ephemeral_key_box=ephemeral_key_box,
                used_saved_key=used_saved_key,
                has_custom_api_key=has_custom_api_key,
                resolved_protocol=resolved_protocol,
                target_models=list(payload.models),
                checks=list(payload.checks),
                timeout_seconds=payload.timeout_seconds,
                concurrency=payload.concurrency,
                total_rounds=payload.rounds,
                cooldown_ms=payload.cooldown_ms,
                initial_profiles=initial_profiles,
                on_case_done=on_case_done,
            ),
            name=f"model-test-{run_id[:8]}",
        )
        self.active[run_id] = task

        def _on_task_done(_completed: asyncio.Task[Any]) -> None:
            ephemeral_key_box[0] = ""
            self.active.pop(run_id, None)
            self.live.pop(run_id, None)
            self._cancel_reasons.pop(run_id, None)
            live.terminal = True
            live.notify()

        task.add_done_callback(_on_task_done)
        return initial_detail

    async def _cooldown(self, live: LiveModelTestRun, cooldown_ms: int) -> None:
        """轮间可中断短切片冷却，确保取消请求能在百毫秒内响应。"""
        live.stage = "cooling_down"
        live.current_models = []
        remaining = int(cooldown_ms)
        live.cooldown_remaining_ms = remaining
        live.notify()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (remaining / 1000.0)
        while True:
            left_s = deadline - loop.time()
            if left_s <= 0:
                break
            await asyncio.sleep(min(COOLDOWN_SLICE_SECONDS, left_s))
            left_ms = max(0, int(round((deadline - loop.time()) * 1000)))
            if left_ms != live.cooldown_remaining_ms:
                live.cooldown_remaining_ms = left_ms
                live.notify()
        live.cooldown_remaining_ms = None
        live.stage = "executing"
        live.notify()

    async def _run_worker(
        self,
        *,
        run_id: str,
        live: LiveModelTestRun,
        base_url: str,
        ephemeral_key_box: list[str],
        used_saved_key: bool,
        has_custom_api_key: bool,
        resolved_protocol: ResolvedModelProtocol,
        target_models: list[str],
        checks: list[ProbeCheckName],
        timeout_seconds: float,
        concurrency: int,
        total_rounds: int,
        cooldown_ms: int,
        initial_profiles: dict[str, ModelProfile],
        on_case_done: Callable[[dict[str, Any]], Any] | None,
    ) -> None:
        """执行单轮或多轮模型并发探测，持续落库并在 finally 中回收全部句柄与临时密钥。"""
        ephemeral_key = ephemeral_key_box[0]
        ephemeral_key_box[0] = ""
        extra_secrets: tuple[str, ...] = (ephemeral_key,) if ephemeral_key else ()
        secret_ctx = _secret_context(extra_secrets)
        headers = build_protocol_headers(resolved_protocol, ephemeral_key)
        check_set = set(checks)
        trust_env = bool(getattr(self.settings, "trust_system_proxy", False))
        worker_count = min(max(1, concurrency), max(1, len(target_models)))

        latest_profiles: dict[str, ModelProfile] = {
            mid: prof.model_copy(deep=True) for mid, prof in initial_profiles.items()
        }
        sub_cases: list[dict[str, Any]] = []
        rounds_summary: list[dict[str, Any]] = []
        completed_rounds = 0
        state_lock = asyncio.Lock()

        def _ordered_snapshot_dicts() -> list[dict[str, Any]]:
            raw = [
                latest_profiles[mid].model_dump(mode="json")
                for mid in target_models
                if mid in latest_profiles
            ]
            return secret_ctx.clean(raw) if secret_ctx.redactor.secrets else raw

        try:
            async with llm_testing.httpx.AsyncClient(
                timeout=timeout_seconds, trust_env=trust_env
            ) as client:
                for round_idx in range(1, total_rounds + 1):
                    live.current_round = round_idx
                    live.stage = "executing"
                    live.cooldown_remaining_ms = None
                    live.notify()

                    round_profiles: dict[str, ModelProfile] = {}
                    round_latencies: list[float] = []
                    semaphore = asyncio.Semaphore(worker_count)

                    async def _probe_one(
                        case_index: int,
                        model_id: str,
                        current_round: int = round_idx,
                        done_rounds: int = completed_rounds,
                        sem: asyncio.Semaphore = semaphore,
                        round_prof_map: dict[str, ModelProfile] = round_profiles,
                        round_lat_list: list[float] = round_latencies,
                    ) -> None:
                        async with sem:
                            async with state_lock:
                                if model_id not in live.current_models:
                                    live.current_models = [*live.current_models, model_id]
                                live.notify()

                            base_profile = initial_profiles.get(model_id)
                            profile = await probe_single_model(
                                client,
                                base_url,
                                headers,
                                model_id,
                                check_set,
                                resolved_protocol,
                                initial_profile=base_profile.model_copy(deep=True)
                                if base_profile is not None
                                else None,
                            )

                            observation = build_probe_observation(
                                profile,
                                base_url=base_url,
                                protocol=resolved_protocol,
                                checks=checks,
                            )
                            sub_status = _derive_sub_case_status(profile, check_set)
                            case_id = f"r{current_round}:{model_id}"
                            label = (
                                f"第 {current_round} 轮 · {model_id}"
                                if total_rounds > 1
                                else model_id
                            )
                            request_snapshot = {
                                "base_url": base_url,
                                "protocol": resolved_protocol,
                                "endpoint_path": observation["endpoint_path"],
                                "model_id": model_id,
                                "checks": list(checks),
                                "timeout_seconds": timeout_seconds,
                                "used_saved_key": used_saved_key,
                                "has_custom_api_key": has_custom_api_key,
                                "round_index": current_round,
                            }
                            sub_case_model = ModelTestSubCaseSnapshot(
                                case_id=case_id,
                                round_index=current_round,
                                case_index=case_index,
                                label=label,
                                model_id=model_id,
                                checks=list(checks),
                                status=sub_status,
                                latency_ms=observation["latency_ms"],
                                ttft_ms=observation["ttft_ms"],
                                request_snapshot=request_snapshot,
                                observation=observation,
                                profile=profile,
                                created_at=utc_now(),
                            )
                            sub_case_dict = sub_case_model.model_dump(mode="json")
                            if secret_ctx.redactor.secrets:
                                sub_case_dict = secret_ctx.clean(sub_case_dict)
                                profile = ModelProfile.model_validate(sub_case_dict["profile"])

                            case_link = build_case_link_block(
                                run_id=run_id,
                                case_id=case_id,
                                round_index=current_round,
                                label=label,
                                status=sub_status,
                            )

                            async with state_lock:
                                live.current_models = [
                                    mid for mid in live.current_models if mid != model_id
                                ]
                                latest_profiles[model_id] = profile
                                round_prof_map[model_id] = profile
                                if isinstance(sub_case_dict.get("latency_ms"), (int, float)):
                                    round_lat_list.append(float(sub_case_dict["latency_ms"]))
                                sub_cases.append(sub_case_dict)
                                snapshot_dicts = _ordered_snapshot_dicts()
                                summary_json, summary_text = _build_run_summary(
                                    target_models=target_models,
                                    latest_profiles=latest_profiles,
                                    sub_cases=sub_cases,
                                    check_set=check_set,
                                    completed_rounds=done_rounds,
                                    total_rounds=total_rounds,
                                )
                                live.record["models_snapshot"] = snapshot_dicts
                                live.record["sub_cases"] = copy.deepcopy(sub_cases)
                                live.record["sub_case_count"] = len(sub_cases)
                                live.record["summary"] = summary_json
                                live.record["summary_text"] = summary_text
                                await self.repo.update_progress(
                                    run_id,
                                    completed_rounds=done_rounds,
                                    models_snapshot=snapshot_dicts,
                                    rounds_json=rounds_summary,
                                    sub_cases_json=sub_cases,
                                    summary_json=summary_json,
                                    summary_text=summary_text,
                                    extra_secrets=extra_secrets,
                                )
                                live.notify()

                            await _invoke_callback(on_case_done, case_link)

                    probe_tasks = [
                        asyncio.create_task(_probe_one(idx, mid))
                        for idx, mid in enumerate(target_models)
                    ]
                    try:
                        await asyncio.gather(*probe_tasks)
                    finally:
                        for task in probe_tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*probe_tasks, return_exceptions=True)

                    completed_rounds = round_idx
                    round_entry = {
                        "round": round_idx,
                        "tested_count": len(round_profiles),
                        "available_count": sum(
                            1
                            for p in round_profiles.values()
                            if p.availability.status == "available"
                        ),
                        "error_count": sum(
                            1 for p in round_profiles.values() if p.availability.status == "error"
                        ),
                        "case_ids": [f"r{round_idx}:{mid}" for mid in target_models],
                        "avg_latency_ms": round(sum(round_latencies) / len(round_latencies), 2)
                        if round_latencies
                        else None,
                    }
                    rounds_summary.append(round_entry)
                    snapshot_dicts = _ordered_snapshot_dicts()
                    summary_json, summary_text = _build_run_summary(
                        target_models=target_models,
                        latest_profiles=latest_profiles,
                        sub_cases=sub_cases,
                        check_set=check_set,
                        completed_rounds=completed_rounds,
                        total_rounds=total_rounds,
                    )
                    live.record["completed_rounds"] = completed_rounds
                    live.record["rounds"] = copy.deepcopy(rounds_summary)
                    live.record["summary"] = summary_json
                    live.record["summary_text"] = summary_text
                    await self.repo.update_progress(
                        run_id,
                        completed_rounds=completed_rounds,
                        models_snapshot=snapshot_dicts,
                        rounds_json=rounds_summary,
                        sub_cases_json=sub_cases,
                        summary_json=summary_json,
                        summary_text=summary_text,
                        extra_secrets=extra_secrets,
                    )
                    live.notify()

                    if round_idx < total_rounds and cooldown_ms > 0:
                        await self._cooldown(live, cooldown_ms)

            snapshot_dicts = _ordered_snapshot_dicts()
            summary_json, summary_text = _build_run_summary(
                target_models=target_models,
                latest_profiles=latest_profiles,
                sub_cases=sub_cases,
                check_set=check_set,
                completed_rounds=completed_rounds,
                total_rounds=total_rounds,
            )
            final_detail = await self.repo.finish_run(
                run_id,
                status="completed",
                completed_rounds=completed_rounds,
                models_snapshot=snapshot_dicts,
                rounds_json=rounds_summary,
                sub_cases_json=sub_cases,
                summary_json=summary_json,
                summary_text=summary_text,
                error_message=None,
                extra_secrets=extra_secrets,
            )
            live.record = final_detail
            live.stage = "finished"
            live.terminal = True
        except asyncio.CancelledError:
            cancel_reason = self._cancel_reasons.get(run_id) or "任务已取消"
            snapshot_dicts = _ordered_snapshot_dicts()
            summary_json, summary_text = _build_run_summary(
                target_models=target_models,
                latest_profiles=latest_profiles,
                sub_cases=sub_cases,
                check_set=check_set,
                completed_rounds=completed_rounds,
                total_rounds=total_rounds,
            )
            try:
                final_detail = await self.repo.finish_run(
                    run_id,
                    status="cancelled",
                    completed_rounds=completed_rounds,
                    models_snapshot=snapshot_dicts,
                    rounds_json=rounds_summary,
                    sub_cases_json=sub_cases,
                    summary_json=summary_json,
                    summary_text=summary_text,
                    error_message=cancel_reason,
                    extra_secrets=extra_secrets,
                )
                live.record = final_detail
            except Exception:  # noqa: BLE001
                live.record["status"] = "cancelled"
                live.record["error_message"] = cancel_reason
            live.stage = "finished"
            live.terminal = True
        except Exception as exc:  # noqa: BLE001 - 后台任务异常落库而非未处理崩溃
            raw_error = f"{type(exc).__name__}: {exc}"
            clean_error = secret_ctx.text(raw_error) if secret_ctx.redactor.secrets else raw_error
            snapshot_dicts = _ordered_snapshot_dicts()
            summary_json, summary_text = _build_run_summary(
                target_models=target_models,
                latest_profiles=latest_profiles,
                sub_cases=sub_cases,
                check_set=check_set,
                completed_rounds=completed_rounds,
                total_rounds=total_rounds,
            )
            try:
                final_detail = await self.repo.finish_run(
                    run_id,
                    status="error",
                    completed_rounds=completed_rounds,
                    models_snapshot=snapshot_dicts,
                    rounds_json=rounds_summary,
                    sub_cases_json=sub_cases,
                    summary_json=summary_json,
                    summary_text=summary_text,
                    error_message=clean_error,
                    extra_secrets=extra_secrets,
                )
                live.record = final_detail
            except Exception:  # noqa: BLE001
                live.record["status"] = "error"
                live.record["error_message"] = clean_error
            live.stage = "finished"
            live.terminal = True
        finally:
            # 显式清空内存中的临时密钥、请求头和活跃句柄。
            ephemeral_key = ""
            ephemeral_key_box[0] = ""
            extra_secrets = ()
            headers.clear()
            live.current_models = []
            live.cooldown_remaining_ms = None
            live.stage = "finished"
            live.terminal = True
            self.active.pop(run_id, None)
            self.live.pop(run_id, None)
            self._cancel_reasons.pop(run_id, None)
            live.notify()

    async def subscribe(
        self,
        run_id: str,
        *,
        heartbeat_seconds: float = 10.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """可重连的 NDJSON 进度流：先回放当前快照，再推送增量 progress/cooling_down/done 帧。

        客户端断开观察流时仅退出本生成器，绝不取消后台探测任务。
        """
        live = self.live.get(run_id)
        if live is None:
            stored = await self.repo.get_run(run_id)
            if stored is None:
                raise LookupError("model_test_run_not_found")
            static_live = LiveModelTestRun(stored)
            static_live.stage = "finished"
            static_live.terminal = True
            frame = static_live.snapshot_frame(sent_cases=0)
            yield {"type": "done", "snapshot": True, **frame, "run": stored}
            return

        sent_cases = 0
        first = True
        while True:
            changed = live.changed
            frame = live.snapshot_frame(sent_cases=sent_cases)
            sent_cases += len(frame["new_sub_cases"])
            terminal = live.terminal or frame["status"] not in {"queued", "running"}

            if terminal:
                latest_run = await self.repo.get_run(run_id) or copy.deepcopy(live.record)
                yield {
                    "type": "done",
                    **({"snapshot": True} if first else {}),
                    **frame,
                    "run": latest_run,
                }
                return

            frame_type = "cooling_down" if frame["stage"] == "cooling_down" else "progress"
            if first:
                yield {"type": frame_type, "snapshot": True, **frame}
                first = False
            else:
                yield {"type": frame_type, **frame}

            try:
                await asyncio.wait_for(changed.wait(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield {"type": "heartbeat"}
                continue

            if run_id not in self.live and not live.terminal:
                stored = await self.repo.get_run(run_id)
                if stored is not None and stored.get("status") not in {"queued", "running"}:
                    live.record = stored
                    live.stage = "finished"
                    live.terminal = True

    async def wait_for_completion(self, run_id: str) -> dict[str, Any]:
        """事件驱动等待指定任务完成，返回持久化终态详情；等待方取消不影响后台任务。"""
        task = self.active.get(run_id)
        if task is not None and not task.done():
            await _await_task_done(task)
        stored = await self.repo.get_run(run_id)
        if stored is None:
            raise LookupError("model_test_run_not_found")
        return stored

    async def cancel(self, run_id: str, *, reason: str = "任务已取消") -> dict[str, Any]:
        """显式取消指定模型探测任务，等待后台收尾并保留已完成子用例。"""
        task = self.active.get(run_id)
        if task is not None and not task.done():
            self._cancel_reasons[run_id] = reason
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        stored = await self.repo.get_run(run_id)
        if stored is None:
            raise LookupError("model_test_run_not_found")
        if stored["status"] in {"queued", "running"}:
            stored = await self.repo.finish_run(
                run_id,
                status="cancelled",
                error_message=reason,
            )
            self.active.pop(run_id, None)
            live = self.live.pop(run_id, None)
            if live is not None:
                live.record = stored
                live.stage = "finished"
                live.terminal = True
                live.notify()
        return stored

    def active_runs(self) -> list[dict[str, Any]]:
        """返回当前所有运行中的模型探测任务摘要列表，供跨页切回重连。"""
        items: list[dict[str, Any]] = []
        for run_id, live in list(self.live.items()):
            if run_id in self.active and not live.terminal:
                items.append(live.active_item())
        return items

    def is_active(self, run_id: str) -> bool:
        """判断指定任务当前是否处于活跃执行状态。"""
        task = self.active.get(run_id)
        return task is not None and not task.done()

    async def delete_run(self, run_id: str) -> bool:
        """仅允许删除终态任务；若任务仍在运行则抛出 ``ModelTestRunActiveError``。"""
        if self.is_active(run_id):
            raise ModelTestRunActiveError("model_test_run_active")
        stored = await self.repo.get_run(run_id)
        if stored is None:
            return False
        if stored.get("status") in {"queued", "running"}:
            raise ModelTestRunActiveError("model_test_run_active")
        return await self.repo.delete_run(run_id)

    async def clear_terminal_runs(self) -> list[str]:
        """清理所有终态历史记录（自动跳过活跃任务），返回被删除的任务 ID 列表。"""
        return await self.repo.clear_terminal_runs(exclude_ids=set(self.active.keys()))

    async def recover(self) -> int:
        """应用启动恢复：将遗留的 queued/running 任务标记为 interrupted。"""
        return await self.repo.recover()

    async def shutdown(self) -> None:
        """应用关闭时取消并回收全部在跑模型探测任务及底层 httpx 资源。"""
        active_ids = list(self.active.keys())
        if not active_ids:
            return
        await asyncio.gather(
            *(self.cancel(run_id, reason="服务关闭导致任务取消") for run_id in active_ids),
            return_exceptions=True,
        )
