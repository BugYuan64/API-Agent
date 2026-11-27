"""Decoupled task document recognition, case drafting and AI catalog planning service."""

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from api_agent.agent.service import AgentError, AgentService, CallBudget
from api_agent.tasks.documents import recognize
from api_agent.tasks.generation import generate_cases, operation_key, with_operations
from api_agent.tasks.schemas import Catalog, RecognizeDocument, RegenerateTests

MIN_TESTS_PER_URL = 1
MAX_TESTS_PER_URL = 10


def contract_map(catalog: Catalog, contracts: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """Index source constraints by operation identity so selection cannot misalign them."""
    return {
        operation_key(endpoint): contract
        for endpoint, contract in zip(catalog.endpoints, contracts, strict=False)
    }


class GeneratedTitle(BaseModel):
    """A title-only model response for standard documents without a title."""

    title: str = Field(min_length=1, max_length=160)


def _has_model_client(agent: Any) -> bool:
    """Distinguish a configured AgentService or test double from an unconfigured/absent agent."""
    if agent is None:
        return False
    if isinstance(agent, AgentService):
        return agent.client is not None
    return getattr(agent, "client", True) is not None


def _require_agent(agent: Any) -> Any:
    """Require an active model client before invoking structured model stages."""
    if not _has_model_client(agent):
        raise AgentError("model_not_configured")
    return agent


def _clamp_tests_per_url(tests_per_url: int) -> int:
    """Validate integer quota and clamp to the canonical 1..10 range."""
    if isinstance(tests_per_url, bool) or not isinstance(tests_per_url, int):
        raise ValueError("invalid_tests_per_url")
    return max(MIN_TESTS_PER_URL, min(MAX_TESTS_PER_URL, tests_per_url))


def _combine_instructions(*parts: str | None) -> str | None:
    """Combine non-empty instruction fragments into one bounded prompt guidance string."""
    cleaned: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text = part.strip()
            if text and text not in cleaned:
                cleaned.append(text)
    if not cleaned:
        return None
    return "\n".join(cleaned)[:2000]


async def fill_catalog_title(agent: Any, catalog: Catalog) -> None:
    """Keep local recognition usable when the optional title model is unavailable."""
    if catalog.title:
        return
    cases = catalog.endpoints or catalog.cases
    if not cases:
        return
    operations = [f"{case.method} {case.path} {case.name}" for case in cases[:12]]
    if _has_model_client(agent):
        try:
            async with asyncio.timeout(15):
                result = await agent.structured(
                    "task_title", {"operations": operations}, GeneratedTitle, CallBudget(limit=1)
                )
            catalog.title = Catalog.compact_title(result.title)
        except (AgentError, TimeoutError):
            pass
    if not catalog.title:
        catalog.title = Catalog.compact_title(cases[0].name)


class TaskPlanningService:
    """Orchestrate document parsing, title inference, incremental merge and AI case drafting."""

    async def recognize_and_draft(
        self,
        agent: Any,
        payload: RecognizeDocument,
        *,
        instructions: str | None = None,
    ) -> Catalog:
        """Recognize standard documents locally; fall back to LLM and draft cases when requested."""
        contracts: list[dict[str, Any]] = []
        catalog = recognize(payload.text, operations=contracts if payload.tests_per_url else None)
        if catalog is None:
            active_agent = _require_agent(agent)
            async with asyncio.timeout(120):
                catalog = await active_agent.structured(
                    "recognize", {"contract": payload.text}, Catalog, CallBudget(limit=2)
                )
            if catalog.server_url and catalog.server_url not in payload.text:
                catalog.server_url = None
            catalog.title = catalog.title or Catalog.compact_title(
                (catalog.endpoints or catalog.cases)[0].name
            )
        else:
            if payload.previous and payload.previous.title:
                catalog.title = catalog.title or payload.previous.title
            else:
                await fill_catalog_title(agent, catalog)

        if payload.tests_per_url is not None:
            indexed = contract_map(catalog, contracts)
            endpoints = with_operations(catalog)
            previous = (
                {operation_key(item): item for item in payload.previous.endpoints}
                if payload.previous
                else {}
            )
            selected = {
                index for index, item in enumerate(endpoints) if operation_key(item) not in previous
            }
            catalog = Catalog.model_validate(
                {
                    **catalog.model_dump(),
                    "endpoints": [previous.get(operation_key(item), item) for item in endpoints],
                    "cases": [],
                }
            )
            if selected:
                active_agent = _require_agent(agent)
                async with asyncio.timeout(240):
                    catalog = await generate_cases(
                        active_agent,
                        catalog,
                        indexed,
                        payload.tests_per_url,
                        selected,
                        instructions=instructions,
                    )
        return catalog

    async def regenerate_selected(
        self,
        agent: Any,
        payload: RegenerateTests,
        *,
        instructions: str | None = None,
    ) -> Catalog:
        """Regenerate only the selected operations while keeping untouched groups intact."""
        if any(not 0 <= index < len(payload.catalog.endpoints) for index in payload.selected):
            raise ValueError("unknown_operation")
        contracts: list[dict[str, Any]] = []
        source = (
            recognize(payload.document.text, operations=contracts) if payload.document else None
        )
        original_tests = {
            operation_key(endpoint): endpoint.generated_cases
            for endpoint in (source.endpoints if source else [])
        }
        selected = set(payload.selected)
        catalog = payload.catalog.model_copy(
            update={
                "endpoints": [
                    endpoint.model_copy(
                        update={"generated_cases": original_tests.get(operation_key(endpoint), [])}
                    )
                    if index in selected
                    else endpoint
                    for index, endpoint in enumerate(payload.catalog.endpoints)
                ]
            }
        )
        active_agent = _require_agent(agent)
        async with asyncio.timeout(240):
            return await generate_cases(
                active_agent,
                catalog,
                contract_map(source, contracts) if source else {},
                payload.tests_per_url,
                selected,
                instructions=instructions,
            )

    async def build_ai_catalog(
        self,
        agent: Any,
        *,
        origin: str | None,
        prompt_or_doc: str,
        tests_per_url: int,
        base_catalog: Catalog | None = None,
        instructions: str | None = None,
    ) -> Catalog:
        """Build or refine a smart-group-testing Catalog strictly honoring tests_per_url (1..10)."""
        count = _clamp_tests_per_url(tests_per_url)
        raw_text = (prompt_or_doc or "").strip()

        if base_catalog is not None:
            contracts: list[dict[str, Any]] = []
            source = recognize(raw_text, operations=contracts) if raw_text else None
            indexed = contract_map(source, contracts) if source is not None else {}
            effective_instructions = (
                _combine_instructions(instructions)
                if source is not None
                else _combine_instructions(instructions, raw_text)
            )
            endpoints = [
                endpoint.model_copy(update={"generated_cases": []})
                for endpoint in with_operations(base_catalog)
            ]
            catalog = Catalog.model_validate(
                {
                    **base_catalog.model_dump(),
                    "server_url": base_catalog.server_url or Catalog.safe_server_hint(origin),
                    "endpoints": endpoints,
                    "cases": [],
                }
            )
            await fill_catalog_title(agent, catalog)
            active_agent = _require_agent(agent)
            async with asyncio.timeout(240):
                return await generate_cases(
                    active_agent,
                    catalog,
                    indexed,
                    count,
                    selected=set(range(len(catalog.endpoints))),
                    instructions=effective_instructions,
                )

        if not raw_text:
            raise ValueError("empty_prompt_or_document")

        is_standard_doc = recognize(raw_text) is not None
        effective_instructions = (
            _combine_instructions(instructions)
            if is_standard_doc
            else _combine_instructions(instructions, raw_text)
        )
        payload = RecognizeDocument(
            filename="ai-document.txt",
            text=raw_text,
            tests_per_url=count,
        )
        catalog = await self.recognize_and_draft(
            agent,
            payload,
            instructions=effective_instructions,
        )
        if not catalog.server_url and origin:
            hint = Catalog.safe_server_hint(origin)
            if hint:
                catalog = catalog.model_copy(update={"server_url": hint})
        return catalog
