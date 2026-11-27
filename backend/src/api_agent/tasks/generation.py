"""Prepare editable functional cases from recognized operations without dispatching requests."""

import asyncio
import json
import re
from itertools import chain
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from pydantic import BaseModel, Field, JsonValue, field_validator

from api_agent.agent.service import AgentError, CallBudget
from api_agent.console.service import ConsoleRequest
from api_agent.infrastructure.logging import is_sensitive_key
from api_agent.tasks.schemas import Case, Catalog, Check, Endpoint, ExtractRule

MAX_GENERATION_CALLS = 16
MAX_INSTRUCTIONS_CHARS = 2000


class TestShortfall(ValueError):
    """A generation request exhausted its budget before reaching the requested quota."""


class DraftInput(BaseModel):
    """Model-provided input may vary per case. Transport identity stays with its operation."""

    name: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=1800)
    headers: dict[str, str] = Field(default_factory=dict)
    body: JsonValue = None
    messages: list[str] = Field(default_factory=list, max_length=20)
    assertions: list[Check] = Field(default_factory=list, max_length=20)
    extract: list[ExtractRule] = Field(default_factory=list, max_length=10)

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """Apply the same header validation policy as interactive requests."""
        return ConsoleRequest.safe_headers(value)


class DraftGroup(BaseModel):
    cases: list[DraftInput] = Field(max_length=10)


class DraftBatch(BaseModel):
    groups: list[DraftGroup] = Field(max_length=8)


def operation_key(case):
    """Match an operation across parsing and editing without using its example values."""
    template = getattr(case, "path_template", None)
    route = template.split("?", 1)[0] if template else urlsplit(case.path).path
    return (
        case.protocol,
        case.method,
        route,
    )


def with_operations(catalog):
    """Assign standalone requests to their operations exactly once before drafting tests."""
    endpoints = list(catalog.endpoints)
    for case in catalog.cases:
        exact = [
            index
            for index, item in enumerate(endpoints)
            if operation_key(item) == operation_key(case)
        ]
        matches = exact or [
            index for index, item in enumerate(endpoints) if same_operation(item, case)
        ]
        if len(matches) == 1:
            index = matches[0]
            endpoints[index] = endpoints[index].model_copy(
                update={"cases": [*endpoints[index].cases, case]}
            )
        else:
            endpoints.append(Endpoint(**case.model_dump()))
    return endpoints


def matches_path(endpoint, path):
    """Match a concrete request against the endpoint's declared route template."""
    route_template = (endpoint.path_template or endpoint.path).split("?", 1)[0]
    parts = re.split(r"(\{[^{}]+\})", route_template)
    pattern = "".join(r"[^/{}]+" if part.startswith("{") else re.escape(part) for part in parts)
    return re.fullmatch(pattern, urlsplit(path).path) is not None


def same_operation(endpoint, case):
    """Associate independent document cases with the operation that owns them."""
    return operation_key(endpoint)[:2] == operation_key(case)[:2] and matches_path(
        endpoint, case.path
    )


def _merge_draft_headers(
    endpoint_headers: dict[str, str], draft_headers: dict[str, str]
) -> dict[str, str]:
    """Merge safe draft headers onto endpoint headers without overriding secret headers."""
    merged = dict(endpoint_headers)
    if not draft_headers:
        return merged
    existing_lower = {key.lower(): key for key in merged}
    protected_lower = {key.lower() for key in endpoint_headers if is_sensitive_key(key)}
    for key, value in draft_headers.items():
        lowered = key.lower()
        if lowered in protected_lower:
            continue
        previous_key = existing_lower.get(lowered)
        if previous_key is not None and previous_key != key:
            merged.pop(previous_key, None)
        merged[key] = value
        existing_lower[lowered] = key
    return ConsoleRequest.safe_headers(merged)


def bind_case(endpoint, draft):
    """Bound generated paths to the declared operation and keep method/protocol fixed."""
    actual = urlsplit(draft.path).path
    if not matches_path(endpoint, draft.path) or any(
        unquote(segment) in {".", ".."} or any(char in unquote(segment) for char in "/\\{}")
        for segment in actual.split("/")
    ):
        raise ValueError("case_outside_operation")
    merged_headers = _merge_draft_headers(endpoint.headers, draft.headers)
    return Case(
        **draft.model_dump(exclude={"headers"}),
        method=endpoint.method,
        protocol=endpoint.protocol,
        headers=merged_headers,
        source="generated",
        kind="test",
    )


def request_key(case):
    """Compare the dispatched inputs, ignoring labels, assertions and JSON field order."""
    url = urlsplit(case.path)
    query = sorted(parse_qsl(url.query, keep_blank_values=True), key=lambda pair: pair[0])
    return (
        case.protocol,
        case.method,
        url.path,
        tuple(query),
        tuple(sorted((name.lower(), value) for name, value in case.headers.items())),
        json.dumps(case.body, sort_keys=True, ensure_ascii=False),
        tuple(case.messages),
    )


def distinct_cases(endpoint, drafts, excluded):
    """Bind drafts and ignore requests already covered by the document or earlier rounds."""
    seen = set(excluded)
    cases = []
    for draft in drafts:
        case = bind_case(endpoint, draft)
        key = request_key(case)
        if key not in seen:
            seen.add(key)
            cases.append(case)
    return cases


def path_examples(endpoint):
    """Vary an existing integer path example for review, without inventing assertions."""
    url = urlsplit(endpoint.path)
    template = (endpoint.path_template or "").split("?", 1)[0].split("/")
    concrete = url.path.split("/")
    if len(template) != len(concrete):
        return
    for index, segment in enumerate(template):
        if not re.fullmatch(r"\{[^{}]+\}", segment) or not re.fullmatch(
            r"[0-9]{1,9}", concrete[index]
        ):
            continue
        for offset in range(1, 41):
            value = str(int(concrete[index]) + offset)
            parts = list(concrete)
            parts[index] = value
            path = urlunsplit(("", "", "/".join(parts), url.query, ""))
            if len(path) > 1800:
                break
            yield bind_case(
                endpoint,
                DraftInput(
                    name=f"路径参数 {segment[1:-1][:100]} 样例 {value}（待确认）",
                    path=path,
                    body=endpoint.body,
                    messages=endpoint.messages,
                ),
            )


def _is_parameterless_contract(endpoint, contract):
    """判断 OpenAPI/Swagger 是否显式声明了无任何可变入参的只读接口（如 GET /health）。"""
    if not contract or endpoint.protocol == "websocket":
        return False
    if endpoint.method not in {"GET", "HEAD", "OPTIONS"}:
        return False
    route_template = (endpoint.path_template or endpoint.path).split("?", 1)[0]
    if "{" in route_template:
        return False
    params = contract.get("parameters") or []
    schema = contract.get("json_schema") or {}
    return not params and not schema


async def generate_cases(
    agent,
    catalog,
    contracts,
    count,
    selected=None,
    instructions: str | None = None,
):
    """Fill each operation's AI quota with distinct cases, within a bounded retry loop."""
    endpoints = list(catalog.endpoints)
    selected = set(range(len(endpoints))) if selected is None else set(selected)
    if selected - set(range(len(endpoints))):
        raise ValueError("unknown_operation")
    trimmed_instructions = (
        instructions.strip()[:MAX_INSTRUCTIONS_CHARS]
        if isinstance(instructions, str) and instructions.strip()
        else None
    )
    parameterless = {
        index
        for index in selected
        if _is_parameterless_contract(
            endpoints[index], contracts.get(operation_key(endpoints[index]))
        )
    }
    generatable = selected - parameterless
    total = len(catalog.cases) + sum(
        1
        + len(item.cases)
        + (
            count
            if index in generatable
            else (0 if index in parameterless else len(item.generated_cases))
        )
        for index, item in enumerate(endpoints)
    )
    if total > 200:
        raise ValueError("case_limit_exceeded")
    limit = asyncio.Semaphore(3)
    document_keys = {request_key(case) for case in catalog.cases}
    batch_size = min(8, max(1, 16 // count))

    async def generate_batch(indices):
        """Carry forward accepted drafts and ask the model only for each remaining deficit."""
        batch = [endpoints[index] for index in indices]
        accepted = [[] for _ in batch]
        existing = [
            [
                item,
                *item.cases,
                *(case for case in catalog.cases if same_operation(item, case)),
            ]
            for item in batch
        ]
        excluded = [document_keys | {request_key(case) for case in cases} for cases in existing]
        budget = CallBudget(limit=MAX_GENERATION_CALLS)
        stagnant = 0
        while budget.remaining > 0 and stagnant < 6:
            pending = [
                (index, count - len(cases))
                for index, cases in enumerate(accepted)
                if len(cases) < count
            ]
            if not pending:
                break

            def validate(output, pending=pending):
                if len(output.groups) != len(pending) or any(
                    len(group.cases) > remaining
                    for (_, remaining), group in zip(pending, output.groups, strict=True)
                ):
                    raise ValueError("incorrect_generated_case_count")
                return [
                    distinct_cases(batch[index], group.cases, excluded[index])
                    for (index, _), group in zip(pending, output.groups, strict=True)
                ]

            operations = [
                {
                    **batch[index].model_dump(exclude={"cases", "generated_cases", "headers"}),
                    "contract": contracts.get(operation_key(batch[index]), {}),
                    "remaining_cases": remaining,
                    "existing_inputs": [
                        case.model_dump(include={"path", "body", "messages"})
                        for case in (*existing[index], *accepted[index])
                    ],
                }
                for index, remaining in pending
            ]
            prompt_payload: dict[str, object] = {"operations": operations}
            if trimmed_instructions:
                prompt_payload["instructions"] = trimmed_instructions
            before = sum(map(len, accepted))
            try:
                async with limit:
                    output = await agent.structured(
                        "task_cases",
                        prompt_payload,
                        DraftBatch,
                        budget,
                        validate,
                    )
            except AgentError as error:
                if str(error) == "model_budget_exhausted":
                    break
                if str(error) != "invalid_model_output":
                    raise
                stagnant += 1
                continue
            for (index, _), cases in zip(pending, validate(output), strict=True):
                accepted[index].extend(cases)
                excluded[index].update(request_key(case) for case in cases)
            stagnant = 0 if sum(map(len, accepted)) > before else stagnant + 1
        for index, cases in enumerate(accepted):
            if len(cases) >= count:
                continue
            for candidate in chain(batch[index].generated_cases, path_examples(batch[index])):
                if not same_operation(batch[index], candidate):
                    continue
                key = request_key(candidate)
                if key not in excluded[index]:
                    excluded[index].add(key)
                    cases.append(
                        candidate.model_copy(update={"source": "generated", "kind": "test"})
                    )
                    if len(cases) == count:
                        break
        return list(zip(indices, accepted, strict=True))

    # Stop sibling model calls when any batch fails, and preserve a single stable error.
    indices = sorted(generatable)
    tasks = [
        asyncio.create_task(generate_batch(indices[offset : offset + batch_size]))
        for offset in range(0, len(indices), batch_size)
    ]
    try:
        batches = await asyncio.gather(*tasks) if tasks else []
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    deficits = []
    warnings = list(catalog.warnings)
    for index in sorted(parameterless):
        endpoint = endpoints[index]
        endpoints[index] = endpoint.model_copy(update={"generated_cases": []})
        notice = (
            f"{endpoint.method} {operation_key(endpoint)[2]} 无可变入参，"
            "已保留文档用例与契约示例，跳过重复 test 生成。"
        )
        if notice not in warnings:
            warnings.append(notice)
    for index, cases in (item for batch in batches for item in batch):
        endpoint = endpoints[index]
        if len(cases) < count:
            deficits.append(
                f"{endpoint.method} {operation_key(endpoint)[2]}（{len(cases)}/{count}）"
            )
        endpoints[index] = endpoint.model_copy(update={"generated_cases": cases})
    if deficits:
        remaining = f"等 {len(deficits)} 个接口" if len(deficits) > 3 else ""
        raise TestShortfall(
            f"未能补齐不同的 test：{'、'.join(deficits[:3])}{remaining}。可降低每接口生成数后重试。"
        )
    return Catalog.model_validate(
        {**catalog.model_dump(), "endpoints": endpoints, "warnings": warnings}
    )
