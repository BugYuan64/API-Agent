"""模型测试服务核心框架：多协议模型拉取、类型与模态识别、专用端点及进阶能力探测。

职责边界：
- 仅面向用户提交的 OpenAI 兼容 / Anthropic / Gemini 端点执行只读或轻量探测，不改设置库；
- 复用已保存密钥时遵循同源约束（不同 origin 绝不转发旧密钥）；
- 区分供应商元数据、命名推断与真实探测结果，未实测维度保持 untested。
"""

import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from api_agent.llm.service import same_origin
from api_agent.llm.testing_probes import (
    TINY_PNG_DATA_URI,
    extract_error_detail,
    is_asr_model,
    probe_anthropic_availability,
    probe_anthropic_streaming,
    probe_anthropic_structured_output,
    probe_anthropic_tool_calling,
    probe_anthropic_vision,
    probe_audio,
    probe_gemini_availability,
    probe_gemini_streaming,
    probe_gemini_structured_output,
    probe_gemini_tool_calling,
    probe_gemini_vision,
    probe_image_generation,
    probe_moderation,
    probe_openai_streaming,
    probe_openai_structured_output,
    probe_rerank,
    should_fallback_responses,
)
from api_agent.llm.testing_schemas import (
    CapabilityResult,
    ModalityItem,
    ModelCategory,
    ModelDiscoverResponse,
    ModelProbeResponse,
    ModelProfile,
    ModelProtocol,
    ProbeCheckName,
    ResolvedModelProtocol,
)

__all__ = [
    "build_initial_profile",
    "build_probe_observation",
    "build_protocol_headers",
    "detect_protocol",
    "discover_models",
    "normalize_base_url",
    "probe_models",
    "probe_models_stream",
    "probe_single_model",
    "resolve_probe_endpoint_path",
    "same_origin",
]

SAFE_METADATA_KEYS = (
    "id",
    "name",
    "display_name",
    "displayName",
    "object",
    "owned_by",
    "created",
    "type",
    "model_type",
    "context_length",
    "max_context_length",
    "inputTokenLimit",
    "outputTokenLimit",
    "supportedGenerationMethods",
    "architecture",
    "modalities",
    "input_modalities",
    "output_modalities",
    "supported_parameters",
    "capabilities",
)

PROBE_TOOL_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "probe_ping",
            "description": "Return a short echo value to verify function calling support.",
            "parameters": {
                "type": "object",
                "properties": {"echo": {"type": "string", "description": "Short token to echo"}},
                "required": ["echo"],
            },
        },
    }
]


PROBE_RESPONSES_TOOL_SPEC = [
    {
        "type": "function",
        "name": "probe_ping",
        "description": "Return a short echo value to verify function calling support.",
        "parameters": {
            "type": "object",
            "properties": {"echo": {"type": "string", "description": "Short token to echo"}},
            "required": ["echo"],
        },
    }
]


def _should_fallback_responses(resp: httpx.Response) -> bool:
    """兼容既有内部引用：委托 testing_probes.should_fallback_responses。"""
    return should_fallback_responses(resp)


def detect_protocol(base_url: str, requested: ModelProtocol = "auto") -> ResolvedModelProtocol:
    """解析目标协议：显式指定优先，auto 时按主机与路径特征自动识别 Anthropic / Gemini / OpenAI。"""
    if requested != "auto":
        return requested
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower().rstrip("/")
    if "anthropic.com" in host or path.endswith("/anthropic") or path.endswith("/messages"):
        return "anthropic"
    if (
        "generativelanguage.googleapis.com" in host
        or "/v1beta" in path
        or ":generatecontent" in path
        or ":streamgeneratecontent" in path
    ):
        return "gemini"
    return "openai"


def normalize_base_url(base_url: str | None) -> str | None:
    """规范化用户提交的 Base URL，支持裸域名自动补全协议并剥离常见接口子路径。"""
    if not base_url or not base_url.strip():
        return None
    cleaned = base_url.strip().rstrip("/")
    if any(char.isspace() or ord(char) < 32 for char in cleaned):
        return None
    if "://" not in cleaned:
        if "@" in cleaned or cleaned.startswith("/"):
            return None
        host_part = cleaned.split("/", 1)[0].lower()
        is_local = (
            host_part == "localhost"
            or host_part.startswith("localhost:")
            or host_part.startswith("127.")
            or host_part.startswith("[::1]")
        )
        if not is_local and "." not in host_part:
            return None
        cleaned = f"{'http' if is_local else 'https'}://{cleaned}"
    for suffix in (
        "/chat/completions",
        "/responses",
        "/models",
        "/embeddings",
        "/rerank",
        "/moderations",
        "/images/generations",
        "/audio/speech",
        "/audio/transcriptions",
        "/messages",
    ):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
    try:
        parsed = urlsplit(cleaned)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port == 0
        ):
            return None
    except ValueError:
        return None
    return cleaned


def build_protocol_headers(
    protocol: ResolvedModelProtocol,
    api_key: str | None,
) -> dict[str, str]:
    """按协议构造最小 JSON 请求头，避免把密钥拼进 URL 查询串。"""
    headers = {"Content-Type": "application/json"}
    key = api_key.strip() if api_key and api_key.strip() else ""
    if protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif protocol == "gemini":
        if key:
            headers["x-goog-api-key"] = key
    else:
        if key:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def build_headers(api_key: str | None) -> dict[str, str]:
    """兼容入口：构造 OpenAI 兼容协议的最小 JSON 请求头。"""
    return build_protocol_headers("openai", api_key)


def extract_safe_metadata(raw_item: Any) -> dict[str, Any]:
    """仅保留白名单内的公开模型元字段，过滤掉潜在的私有或异常超长内容。"""
    if not isinstance(raw_item, dict):
        return {}
    return {key: raw_item[key] for key in SAFE_METADATA_KEYS if key in raw_item}


def _collect_modality_hints(raw_item: dict[str, Any], field: str) -> list[str]:
    values: list[str] = []
    direct = raw_item.get(field)
    if isinstance(direct, list):
        values.extend(str(item).lower() for item in direct)
    arch = raw_item.get("architecture")
    if isinstance(arch, dict) and isinstance(arch.get(field), list):
        values.extend(str(item).lower() for item in arch[field])
    modalities = raw_item.get("modalities")
    if isinstance(modalities, dict):
        short_key = "input" if field.startswith("input") else "output"
        if isinstance(modalities.get(short_key), list):
            values.extend(str(item).lower() for item in modalities[short_key])
    return list(dict.fromkeys(values))


def infer_model_category(model_id: str, raw_item: dict[str, Any]) -> tuple[ModelCategory, str]:
    """结合供应商元数据与常见模型命名特征识别模型类型（明确标注来源）。"""
    meta_type = str(raw_item.get("type") or raw_item.get("model_type") or "").lower().strip()
    if meta_type in {
        "chat",
        "reasoning",
        "embedding",
        "rerank",
        "image",
        "audio",
        "video",
        "moderation",
    }:
        return meta_type, "metadata"  # type: ignore[return-value]

    # Gemini /v1beta/models 元数据中的 supportedGenerationMethods
    methods = raw_item.get("supportedGenerationMethods")
    if isinstance(methods, list) and methods:
        method_set = {str(m) for m in methods}
        if "embedContent" in method_set and "generateContent" not in method_set:
            return "embedding", "metadata"
        if "generateContent" in method_set:
            lower_id = model_id.lower()
            if "thinking" in lower_id:
                return "reasoning", "inferred"
            return "chat", "metadata"

    lower = model_id.lower()
    if "rerank" in lower:
        return "rerank", "inferred"
    if any(token in lower for token in ("embed", "bge-", "text-embedding", "e5-", "gte-")):
        return "embedding", "inferred"
    if any(token in lower for token in ("moderation", "omni-moderation", "guard")):
        return "moderation", "inferred"
    if any(
        token in lower
        for token in (
            "dall-e",
            "flux",
            "imagen",
            "sdxl",
            "stable-diffusion",
            "midjourney",
            "gpt-image",
            "wanx",
            "kolors",
        )
    ):
        return "image", "inferred"
    if any(
        token in lower
        for token in (
            "whisper",
            "tts",
            "transcribe",
            "speech",
            "audio",
            "cosyvoice",
            "paraformer",
            "sensevoice",
        )
    ):
        return "audio", "inferred"
    if any(token in lower for token in ("sora", "veo", "kling", "video", "wan2.")):
        return "video", "inferred"
    if re.search(r"(?:^|[/:_-])o[134](?:$|[-_/.])", lower) or any(
        token in lower for token in ("deepseek-r1", "reasoner", "qwq", "thinking", "-r1")
    ):
        return "reasoning", "inferred"
    if any(
        token in lower
        for token in (
            "gpt",
            "claude",
            "gemini",
            "deepseek",
            "qwen",
            "llama",
            "mistral",
            "mixtral",
            "glm",
            "doubao",
            "moonshot",
            "kimi",
            "yi-",
            "ernie",
            "hunyuan",
            "hy4",
            "mimo",
            "step-",
            "minimax",
            "chat",
            "instruct",
        )
    ):
        return "chat", "inferred"
    return "unknown", "unknown"


def infer_modalities(
    model_id: str, model_type: ModelCategory, raw_item: dict[str, Any]
) -> tuple[list[ModalityItem], list[ModalityItem]]:
    """构造初始输入/输出模态清单，优先采纳供应商声明，缺失时按常见家族做保守推断。"""
    lower = model_id.lower()
    meta_in = _collect_modality_hints(raw_item, "input_modalities")
    meta_out = _collect_modality_hints(raw_item, "output_modalities")

    inputs: list[ModalityItem] = []
    outputs: list[ModalityItem] = []

    if meta_in:
        for mod in ("text", "image", "audio", "video", "file"):
            if mod in meta_in:
                inputs.append(
                    ModalityItem(
                        modality=mod,
                        status="supported",
                        source="metadata",
                        detail="来自 /models 元数据",
                    )
                )
    else:
        if model_type in {
            "chat",
            "reasoning",
            "embedding",
            "rerank",
            "image",
            "moderation",
            "unknown",
        }:
            inputs.append(
                ModalityItem(
                    modality="text", status="inferred", source="inferred", detail="按模型类型推断"
                )
            )
        if any(
            token in lower
            for token in (
                "vision",
                "-vl",
                "gpt-4o",
                "gpt-4.1",
                "gpt-4.5",
                "gemini",
                "claude-3",
                "claude-4",
                "claude-sonnet",
                "claude-opus",
                "qvq",
                "pixtral",
                "omni",
            )
        ):
            inputs.append(
                ModalityItem(
                    modality="image",
                    status="inferred",
                    source="inferred",
                    detail="命名疑似多模态，待实测",
                )
            )
        if model_type == "audio" or "omni" in lower or "gemini" in lower:
            inputs.append(
                ModalityItem(
                    modality="audio",
                    status="inferred",
                    source="inferred",
                    detail="命名疑似音频输入，待实测",
                )
            )
        if "gemini" in lower or "omni" in lower:
            inputs.append(
                ModalityItem(
                    modality="video",
                    status="inferred",
                    source="inferred",
                    detail="命名疑似视频输入，待实测",
                )
            )

    if meta_out:
        for mod in ("text", "image", "audio", "embedding", "reasoning", "json"):
            if mod in meta_out:
                outputs.append(
                    ModalityItem(
                        modality=mod,
                        status="supported",
                        source="metadata",
                        detail="来自 /models 元数据",
                    )
                )
    else:
        if model_type == "embedding":
            outputs.append(
                ModalityItem(
                    modality="embedding", status="inferred", source="inferred", detail="向量输出"
                )
            )
        elif model_type in {"rerank", "moderation"}:
            outputs.append(
                ModalityItem(
                    modality="json", status="inferred", source="inferred", detail="结构化结果输出"
                )
            )
        elif model_type == "image":
            outputs.append(
                ModalityItem(
                    modality="image", status="inferred", source="inferred", detail="图像生成输出"
                )
            )
        elif model_type == "audio":
            outputs.append(
                ModalityItem(
                    modality="audio", status="inferred", source="inferred", detail="音频/转写输出"
                )
            )
        elif model_type == "reasoning":
            outputs.append(
                ModalityItem(
                    modality="text", status="inferred", source="inferred", detail="文本输出"
                )
            )
            outputs.append(
                ModalityItem(
                    modality="reasoning", status="inferred", source="inferred", detail="推理链输出"
                )
            )
        elif model_type in {"chat", "unknown"}:
            outputs.append(
                ModalityItem(
                    modality="text", status="inferred", source="inferred", detail="文本输出"
                )
            )

    return inputs, outputs


def build_initial_profile(
    raw_item: Any, *, protocol: ResolvedModelProtocol = "openai"
) -> ModelProfile | None:
    """将 /models 列表中的单条记录标准化为初始 ModelProfile。"""
    if isinstance(raw_item, str):
        model_id = raw_item.strip()
        item_dict: dict[str, Any] = {}
    elif isinstance(raw_item, dict):
        raw_id = raw_item.get("id") or raw_item.get("name")
        model_id = raw_id.strip() if isinstance(raw_id, str) else ""
        item_dict = raw_item
    else:
        return None
    if protocol == "gemini" and model_id.startswith("models/"):
        model_id = model_id.removeprefix("models/")
    if not model_id:
        return None

    category, type_source = infer_model_category(model_id, item_dict)
    inputs, outputs = infer_modalities(model_id, category, item_dict)

    supported_params = item_dict.get("supported_parameters")
    if isinstance(supported_params, list) and any(
        str(param).lower() in {"tools", "tool_choice", "functions"} for param in supported_params
    ):
        tool_cap = CapabilityResult(
            status="inferred", source="metadata", detail="元数据声明支持 tools 参数，待实测"
        )
    else:
        tool_cap = CapabilityResult(status="untested", source="untested")

    created_raw = item_dict.get("created")
    created_val = created_raw if type(created_raw) is int else None
    owned_raw = item_dict.get("owned_by")
    owned_val = owned_raw.strip() if isinstance(owned_raw, str) and owned_raw.strip() else None
    if not owned_val and protocol == "anthropic":
        owned_val = "anthropic"
    elif not owned_val and protocol == "gemini":
        owned_val = "google"

    return ModelProfile(
        id=model_id,
        owned_by=owned_val,
        created=created_val,
        model_type=category,
        type_source=type_source,  # type: ignore[arg-type]
        availability=CapabilityResult(status="untested", source="untested"),
        tool_calling=tool_cap,
        streaming=CapabilityResult(status="untested", source="untested"),
        structured_output=CapabilityResult(status="untested", source="untested"),
        input_modalities=inputs,
        output_modalities=outputs,
        raw_metadata=extract_safe_metadata(item_dict),
    )


async def discover_models(
    base_url: str,
    api_key: str | None,
    *,
    protocol: ModelProtocol = "auto",
    timeout_seconds: float = 20.0,
    trust_env: bool = False,
) -> ModelDiscoverResponse:
    """按协议调用 GET /models 验证凭证与地址，缺版本前缀时自动尝试 /v1 或 /v1beta。"""
    started = time.monotonic()
    resolved_protocol = detect_protocol(base_url, protocol)
    candidates = [base_url]
    parsed_path = urlsplit(base_url).path.rstrip("/")
    if resolved_protocol == "gemini":
        if not parsed_path.endswith("/v1beta") and not parsed_path.endswith("/v1"):
            candidates.append(f"{base_url}/v1beta")
    else:
        if not parsed_path.endswith("/v1"):
            candidates.append(f"{base_url}/v1")

    headers = build_protocol_headers(resolved_protocol, api_key)
    resolved_url = base_url
    response: httpx.Response | None = None
    raw: Any = None
    json_error = False

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=trust_env) as client:
            for idx, candidate in enumerate(candidates):
                resolved_url = candidate
                response = await client.get(f"{candidate}/models", headers=headers)
                if response.status_code in {401, 403}:
                    break
                if response.status_code == 200:
                    try:
                        raw = response.json()
                        json_error = False
                        items_preview = (
                            raw.get("data") or raw.get("models") if isinstance(raw, dict) else raw
                        )
                        if isinstance(items_preview, list) or idx == len(candidates) - 1:
                            break
                    except (ValueError, RecursionError):
                        json_error = True
                        if idx == len(candidates) - 1:
                            break
                        continue
                if response.status_code != 404 or idx == len(candidates) - 1:
                    break
    except httpx.TimeoutException:
        elapsed = round((time.monotonic() - started) * 1000, 2)
        return ModelDiscoverResponse(
            ok=False,
            protocol=resolved_protocol,
            base_url=resolved_url,
            latency_ms=elapsed,
            message=f"拉取模型超时（>{timeout_seconds:g}s），请检查服务地址与网络连通性",
        )
    except (httpx.HTTPError, OSError):
        elapsed = round((time.monotonic() - started) * 1000, 2)
        return ModelDiscoverResponse(
            ok=False,
            protocol=resolved_protocol,
            base_url=resolved_url,
            latency_ms=elapsed,
            message="无法连接到指定的 Base URL，请确认地址正确且服务可达",
        )

    assert response is not None
    elapsed = round((time.monotonic() - started) * 1000, 2)
    if response.status_code in {401, 403}:
        return ModelDiscoverResponse(
            ok=False,
            protocol=resolved_protocol,
            base_url=resolved_url,
            http_status=response.status_code,
            latency_ms=elapsed,
            message=f"鉴权失败（HTTP {response.status_code}），请检查 API Key 是否有效",
        )
    if response.status_code != 200:
        return ModelDiscoverResponse(
            ok=False,
            protocol=resolved_protocol,
            base_url=resolved_url,
            http_status=response.status_code,
            latency_ms=elapsed,
            message=f"模型列表接口返回 HTTP {response.status_code}",
        )

    if json_error:
        return ModelDiscoverResponse(
            ok=False,
            protocol=resolved_protocol,
            base_url=resolved_url,
            http_status=response.status_code,
            latency_ms=elapsed,
            message="服务端返回内容不是合法 JSON",
        )

    items = raw.get("data") or raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return ModelDiscoverResponse(
            ok=False,
            protocol=resolved_protocol,
            base_url=resolved_url,
            http_status=response.status_code,
            latency_ms=elapsed,
            message="响应体缺少 data/models 模型数组结构",
        )

    deduped: dict[str, ModelProfile] = {}
    for entry in items:
        profile = build_initial_profile(entry, protocol=resolved_protocol)
        if profile and profile.id not in deduped:
            deduped[profile.id] = profile

    models = sorted(deduped.values(), key=lambda item: item.id.lower())
    return ModelDiscoverResponse(
        ok=True,
        protocol=resolved_protocol,
        base_url=resolved_url,
        http_status=response.status_code,
        latency_ms=elapsed,
        count=len(models),
        message=f"成功拉取 {len(models)} 个模型" if models else "接口连通，但未返回任何模型",
        models=models,
    )


def _upsert_modality(items: list[ModalityItem], updated: ModalityItem) -> list[ModalityItem]:
    result = [item for item in items if item.modality != updated.modality]
    result.append(updated)
    return result


async def _probe_availability(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
    protocol: ResolvedModelProtocol = "openai",
) -> tuple[CapabilityResult, list[ModalityItem], list[ModalityItem]]:
    """探测单个模型基础可用性，覆盖多协议及专用端点，返回 (可用性, 观察输入模态, 观察输出模态)。"""
    if protocol == "anthropic":
        avail, outputs = await probe_anthropic_availability(client, base_url, headers, model_id)
        inputs = (
            [
                ModalityItem(
                    modality="text", status="supported", source="probed", detail="实测支持文本输入"
                )
            ]
            if avail.status == "available"
            else []
        )
        return avail, inputs, outputs

    if protocol == "gemini":
        avail, outputs = await probe_gemini_availability(
            client, base_url, headers, model_id, category
        )
        inputs = (
            [
                ModalityItem(
                    modality="text", status="supported", source="probed", detail="实测支持文本输入"
                )
            ]
            if avail.status == "available"
            else []
        )
        return avail, inputs, outputs

    # OpenAI 兼容协议分支
    started = time.monotonic()
    if category == "embedding":
        try:
            resp = await client.post(
                f"{base_url}/embeddings",
                headers=headers,
                json={"model": model_id, "input": "ping"},
            )
        except httpx.TimeoutException:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    detail="探测超时",
                ),
                [],
                [],
            )
        except (httpx.HTTPError, OSError):
            return (
                CapabilityResult(
                    status="error",
                    source="probed",
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    detail="连接失败",
                ),
                [],
                [],
            )
        elapsed = round((time.monotonic() - started) * 1000, 2)
        if resp.status_code == 200:
            return (
                CapabilityResult(
                    status="available",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Embeddings 端点响应正常",
                ),
                [
                    ModalityItem(
                        modality="text",
                        status="supported",
                        source="probed",
                        detail="实测支持文本输入",
                    )
                ],
                [
                    ModalityItem(
                        modality="embedding",
                        status="supported",
                        source="probed",
                        detail="实测返回向量响应",
                    )
                ],
            )
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"HTTP {resp.status_code}",
            ),
            [],
            [],
        )

    if category == "rerank":
        return await probe_rerank(client, base_url, headers, model_id)
    if category == "moderation":
        return await probe_moderation(client, base_url, headers, model_id)
    if category == "image":
        return await probe_image_generation(client, base_url, headers, model_id)
    if category == "audio":
        return await probe_audio(client, base_url, headers, model_id)

    if category == "video":
        return (
            CapabilityResult(
                status="unsupported",
                source="untested",
                detail="video 类模型暂未接入专用端点探测（框架预留扩展）",
            ),
            [],
            [],
        )

    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "max_tokens": 64,
                "stream": False,
            },
        )
    except httpx.TimeoutException:
        # 推理或封装上下文较重的模型非流式容易超时，自动回退流式首包验证可用性。
        stream_cap = await probe_openai_streaming(client, base_url, headers, model_id, category)
        if stream_cap.status == "supported":
            ttft_hint = (
                f"；首字 {stream_cap.ttft_ms:.0f}ms" if stream_cap.ttft_ms is not None else ""
            )
            return (
                CapabilityResult(
                    status="available",
                    source="probed",
                    latency_ms=stream_cap.latency_ms,
                    ttft_ms=stream_cap.ttft_ms,
                    detail=f"流式对话可用（非流式探测超时，已通过 SSE 验证{ttft_hint}）",
                ),
                [
                    ModalityItem(
                        modality="text",
                        status="supported",
                        source="probed",
                        detail="实测支持文本输入",
                    )
                ],
                [
                    ModalityItem(
                        modality="text",
                        status="supported",
                        source="probed",
                        detail="实测支持流式文本回复",
                    )
                ],
            )
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="对话探测超时（非流式与流式回退均未在超时内返回）",
            ),
            [],
            [],
        )
    except (httpx.HTTPError, OSError):
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="连接失败",
            ),
            [],
            [],
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if _should_fallback_responses(resp):
        avail, outputs = await _probe_availability_responses(client, base_url, headers, model_id)
        inputs = (
            [
                ModalityItem(
                    modality="text", status="supported", source="probed", detail="实测支持文本输入"
                )
            ]
            if avail.status == "available"
            else []
        )
        return avail, inputs, outputs
    if resp.status_code in {401, 403}:
        err_hint = extract_error_detail(resp)
        detail = f"鉴权失败（HTTP {resp.status_code}）"
        if err_hint:
            detail = f"{detail} · {err_hint}"
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=detail,
            ),
            [],
            [],
        )
    if resp.status_code != 200:
        err_hint = extract_error_detail(resp)
        detail = f"HTTP {resp.status_code}"
        if err_hint:
            detail = f"{detail} · {err_hint}"
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=detail,
            ),
            [],
            [],
        )

    try:
        body = resp.json()
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=elapsed,
                    detail="响应缺少 choices",
                ),
                [],
                [],
            )
        msg = choices[0].get("message") or {}
        out_mods = [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本回复"
            )
        ]
        if msg.get("reasoning_content") or msg.get("reasoning"):
            out_mods.append(
                ModalityItem(
                    modality="reasoning",
                    status="supported",
                    source="probed",
                    detail="实测返回 reasoning 字段",
                )
            )
        return (
            CapabilityResult(
                status="available",
                source="probed",
                latency_ms=elapsed,
                detail="Chat Completions 可用",
            ),
            [
                ModalityItem(
                    modality="text", status="supported", source="probed", detail="实测支持文本输入"
                )
            ],
            out_mods,
        )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="unavailable", source="probed", latency_ms=elapsed, detail="返回结构非法"
            ),
            [],
            [],
        )


async def _probe_availability_responses(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, list[ModalityItem]]:
    """当站点停用 /chat/completions 时，自动回退至 OpenAI Responses API（/responses）。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/responses",
            headers=headers,
            json={"model": model_id, "input": "Reply with OK", "max_output_tokens": 64},
        )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="unavailable",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="Responses API 探测超时",
        ), []
    except (httpx.HTTPError, OSError):
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="连接失败",
        ), []

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if resp.status_code != 200:
        err_hint = extract_error_detail(resp)
        detail = f"Responses API 返回 HTTP {resp.status_code}"
        if err_hint:
            detail = f"{detail} · {err_hint}"
        return CapabilityResult(
            status="unavailable",
            source="probed",
            latency_ms=elapsed,
            detail=detail,
        ), []

    try:
        body = resp.json()
        output = body.get("output")
        has_content = isinstance(output, list) and len(output) > 0 or bool(body.get("output_text"))
        if not has_content:
            return CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail="Responses API 响应缺少 output",
            ), []
        out_mods = [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本回复"
            )
        ]
        if isinstance(output, list) and any(
            isinstance(item, dict) and item.get("type") == "reasoning" for item in output
        ):
            out_mods.append(
                ModalityItem(
                    modality="reasoning",
                    status="supported",
                    source="probed",
                    detail="实测返回 reasoning 节点（Responses API）",
                )
            )
        return CapabilityResult(
            status="available",
            source="probed",
            latency_ms=elapsed,
            detail="Responses API 可用（/responses）",
        ), out_mods
    except (ValueError, TypeError, AttributeError):
        return CapabilityResult(
            status="unavailable", source="probed", latency_ms=elapsed, detail="返回结构非法"
        ), []


async def _probe_tool_calling(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
    protocol: ResolvedModelProtocol = "openai",
) -> CapabilityResult:
    """通过声明最小函数工具测试模型是否接受 tools 参数并能生成工具调用。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return CapabilityResult(
            status="unsupported", source="inferred", detail="非对话模型不适用 Function Calling"
        )
    if protocol == "anthropic":
        return await probe_anthropic_tool_calling(client, base_url, headers, model_id)
    if protocol == "gemini":
        return await probe_gemini_tool_calling(client, base_url, headers, model_id, category)

    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "You must call the probe_ping tool with echo='ok'. "
                            "Do not answer directly."
                        ),
                    }
                ],
                "tools": PROBE_TOOL_SPEC,
                "tool_choice": "auto",
                "max_tokens": 64,
                "stream": False,
            },
        )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="工具探测超时",
        )
    except (httpx.HTTPError, OSError):
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="连接失败",
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if _should_fallback_responses(resp):
        return await _probe_tool_calling_responses(client, base_url, headers, model_id)
    if resp.status_code in {400, 404, 422, 501}:
        err_hint = extract_error_detail(resp)
        detail = f"服务端拒绝 tools 参数（HTTP {resp.status_code}）"
        if err_hint:
            detail = f"{detail} · {err_hint}"
        return CapabilityResult(
            status="unsupported",
            source="probed",
            latency_ms=elapsed,
            detail=detail,
        )
    if resp.status_code != 200:
        err_hint = extract_error_detail(resp)
        detail = f"HTTP {resp.status_code}"
        if err_hint:
            detail = f"{detail} · {err_hint}"
        return CapabilityResult(status="error", source="probed", latency_ms=elapsed, detail=detail)

    try:
        body = resp.json()
        choices = body.get("choices") or []
        msg = choices[0].get("message") if choices else {}
        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if isinstance(calls, list) and len(calls) > 0:
            return CapabilityResult(
                status="supported",
                source="probed",
                latency_ms=elapsed,
                detail="实测返回有效 tool_calls",
            )
        return CapabilityResult(
            status="partial",
            source="probed",
            latency_ms=elapsed,
            detail="接受 tools 参数但本次未触发 tool_calls",
        )
    except (ValueError, TypeError, IndexError, AttributeError):
        return CapabilityResult(
            status="error", source="probed", latency_ms=elapsed, detail="工具响应解析失败"
        )


async def _probe_tool_calling_responses(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> CapabilityResult:
    """通过 OpenAI Responses API（/responses）探测函数工具调用能力。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/responses",
            headers=headers,
            json={
                "model": model_id,
                "input": (
                    "You must call the probe_ping tool with echo='ok'. Do not answer directly."
                ),
                "tools": PROBE_RESPONSES_TOOL_SPEC,
                "tool_choice": "auto",
                "max_output_tokens": 64,
            },
        )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="Responses 工具探测超时",
        )
    except (httpx.HTTPError, OSError):
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="连接失败",
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if resp.status_code in {400, 404, 422, 501}:
        return CapabilityResult(
            status="unsupported",
            source="probed",
            latency_ms=elapsed,
            detail=f"Responses 端点拒绝 tools 参数（HTTP {resp.status_code}）",
        )
    if resp.status_code != 200:
        return CapabilityResult(
            status="error", source="probed", latency_ms=elapsed, detail=f"HTTP {resp.status_code}"
        )

    try:
        body = resp.json()
        output = body.get("output") or []
        if isinstance(output, list) and any(
            isinstance(item, dict) and item.get("type") == "function_call" for item in output
        ):
            return CapabilityResult(
                status="supported",
                source="probed",
                latency_ms=elapsed,
                detail="实测返回有效 function_call（/responses）",
            )
        return CapabilityResult(
            status="partial",
            source="probed",
            latency_ms=elapsed,
            detail="Responses 端点接受 tools 但本次未触发调用",
        )
    except (ValueError, TypeError, AttributeError):
        return CapabilityResult(
            status="error", source="probed", latency_ms=elapsed, detail="工具响应解析失败"
        )


async def _probe_vision_input(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
    protocol: ResolvedModelProtocol = "openai",
) -> ModalityItem:
    """用 1x1 透明 PNG 测试模型是否接受图像多模态输入。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return ModalityItem(
            modality="image", status="unsupported", source="inferred", detail="非对话模型"
        )
    if protocol == "anthropic":
        return await probe_anthropic_vision(client, base_url, headers, model_id)
    if protocol == "gemini":
        return await probe_gemini_vision(client, base_url, headers, model_id, category)

    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Reply OK if you received the image."},
                            {"type": "image_url", "image_url": {"url": TINY_PNG_DATA_URI}},
                        ],
                    }
                ],
                "max_tokens": 32,
                "stream": False,
            },
        )
    except (httpx.HTTPError, OSError):
        return ModalityItem(
            modality="image", status="error", source="probed", detail="图像输入探测请求失败"
        )

    if _should_fallback_responses(resp):
        return await _probe_vision_input_responses(client, base_url, headers, model_id)
    if resp.status_code == 200:
        return ModalityItem(
            modality="image", status="supported", source="probed", detail="实测接受 image_url 输入"
        )
    if resp.status_code in {400, 415, 422}:
        return ModalityItem(
            modality="image",
            status="unsupported",
            source="probed",
            detail=f"服务端拒绝图像输入（HTTP {resp.status_code}）",
        )
    return ModalityItem(
        modality="image", status="unknown", source="probed", detail=f"HTTP {resp.status_code}"
    )


async def _probe_vision_input_responses(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> ModalityItem:
    """通过 OpenAI Responses API（/responses）探测 input_image 多模态输入。"""
    try:
        resp = await client.post(
            f"{base_url}/responses",
            headers=headers,
            json={
                "model": model_id,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Reply OK if you received the image."},
                            {"type": "input_image", "image_url": TINY_PNG_DATA_URI},
                        ],
                    }
                ],
                "max_output_tokens": 16,
            },
        )
    except (httpx.HTTPError, OSError):
        return ModalityItem(
            modality="image", status="error", source="probed", detail="图像输入探测请求失败"
        )

    if resp.status_code == 200:
        return ModalityItem(
            modality="image",
            status="supported",
            source="probed",
            detail="实测接受 input_image 输入（/responses）",
        )
    if resp.status_code in {400, 415, 422}:
        return ModalityItem(
            modality="image",
            status="unsupported",
            source="probed",
            detail=f"服务端拒绝图像输入（HTTP {resp.status_code}）",
        )
    return ModalityItem(
        modality="image", status="unknown", source="probed", detail=f"HTTP {resp.status_code}"
    )


async def _probe_streaming(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
    protocol: ResolvedModelProtocol,
) -> CapabilityResult:
    if protocol == "anthropic":
        return await probe_anthropic_streaming(client, base_url, headers, model_id)
    if protocol == "gemini":
        return await probe_gemini_streaming(client, base_url, headers, model_id, category)
    return await probe_openai_streaming(client, base_url, headers, model_id, category)


async def _probe_structured_output(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
    protocol: ResolvedModelProtocol,
) -> tuple[CapabilityResult, ModalityItem | None]:
    if protocol == "anthropic":
        return await probe_anthropic_structured_output(client, base_url, headers, model_id)
    if protocol == "gemini":
        return await probe_gemini_structured_output(client, base_url, headers, model_id, category)
    return await probe_openai_structured_output(client, base_url, headers, model_id, category)


_CANONICAL_CHECK_ORDER: tuple[ProbeCheckName, ...] = (
    "availability",
    "tool_calling",
    "streaming",
    "structured_output",
    "input_modalities",
    "output_modalities",
)


def resolve_probe_endpoint_path(
    protocol: ResolvedModelProtocol,
    category: ModelCategory,
    model_id: str,
    *,
    availability_detail: str | None = None,
    checks: set[ProbeCheckName] | list[ProbeCheckName] | None = None,
) -> str:
    """根据解析协议、模型类别与实际回退结果推导本次探测的主要端点路径（不含密钥）。"""
    if availability_detail and "/responses" in availability_detail:
        return "/responses"
    if protocol == "anthropic":
        return "/messages"
    if protocol == "gemini":
        if category == "embedding":
            return f"/models/{model_id}:embedContent"
        check_set = set(checks or ())
        if check_set == {"streaming"}:
            return f"/models/{model_id}:streamGenerateContent"
        return f"/models/{model_id}:generateContent"
    if category == "embedding":
        return "/embeddings"
    if category == "rerank":
        return "/rerank"
    if category == "moderation":
        return "/moderations"
    if category == "image":
        return "/images/generations"
    if category == "audio":
        return "/audio/transcriptions" if is_asr_model(model_id) else "/audio/speech"
    if category == "video":
        return "/videos"
    return "/chat/completions"


def build_probe_observation(
    profile: ModelProfile,
    *,
    base_url: str,
    protocol: ResolvedModelProtocol,
    checks: list[ProbeCheckName] | set[ProbeCheckName],
) -> dict[str, Any]:
    """生成轻量、脱敏的单模型探测观测摘要，供只读回放与 AI 工具读取。"""
    if isinstance(checks, list):
        ordered_checks = list(dict.fromkeys(checks))
    else:
        ordered_checks = [name for name in _CANONICAL_CHECK_ORDER if name in checks]

    endpoint_path = resolve_probe_endpoint_path(
        protocol,
        profile.model_type,
        profile.id,
        availability_detail=profile.availability.detail,
        checks=ordered_checks,
    )
    latency_ms = next(
        (
            val
            for val in (
                profile.availability.latency_ms,
                profile.streaming.latency_ms,
                profile.tool_calling.latency_ms,
                profile.structured_output.latency_ms,
            )
            if val is not None
        ),
        None,
    )
    ttft_ms = (
        profile.streaming.ttft_ms
        if profile.streaming.ttft_ms is not None
        else profile.availability.ttft_ms
    )
    details = {
        key: value
        for key, value in (
            ("availability", profile.availability.detail),
            ("tool_calling", profile.tool_calling.detail),
            ("streaming", profile.streaming.detail),
            ("structured_output", profile.structured_output.detail),
        )
        if value
    }
    return {
        "protocol": protocol,
        "base_url": base_url,
        "endpoint_path": endpoint_path,
        "model_type": profile.model_type,
        "type_source": profile.type_source,
        "checks_executed": ordered_checks,
        "latency_ms": latency_ms,
        "ttft_ms": ttft_ms,
        "capability_summary": {
            "availability": profile.availability.status,
            "tool_calling": profile.tool_calling.status,
            "streaming": profile.streaming.status,
            "structured_output": profile.structured_output.status,
        },
        "details": details,
    }


async def probe_single_model(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    check_set: set[ProbeCheckName],
    resolved_protocol: ResolvedModelProtocol,
    *,
    initial_profile: ModelProfile | None = None,
) -> ModelProfile:
    """对单个模型执行所选能力探测，隔离单模型异常以免阻塞消费队列中的其他模型。

    传入 ``initial_profile`` 时深拷贝并保留其已发现的 ``raw_metadata``、``owned_by``、
    ``created``、``model_type``、``type_source`` 及初始模态清单，避免覆盖 /models 元数据。
    """
    if initial_profile is not None:
        profile = initial_profile.model_copy(deep=True)
        if not profile.id and model_id.strip():
            profile.id = model_id.strip()
    else:
        profile = build_initial_profile(model_id, protocol=resolved_protocol)
        if profile is None:
            raise ValueError(f"Invalid model_id: {model_id!r}")
    try:
        if "availability" in check_set:
            avail, observed_inputs, observed_outputs = await _probe_availability(
                client, base_url, headers, model_id, profile.model_type, resolved_protocol
            )
            profile.availability = avail
            for mod in observed_inputs:
                profile.input_modalities = _upsert_modality(profile.input_modalities, mod)
            for mod in observed_outputs:
                profile.output_modalities = _upsert_modality(profile.output_modalities, mod)
            if avail.status == "available" and profile.model_type == "unknown":
                has_reasoning = any(
                    mod.modality == "reasoning" and mod.status == "supported"
                    for mod in observed_outputs
                )
                profile.model_type = "reasoning" if has_reasoning else "chat"
                profile.type_source = "probed"
        if "tool_calling" in check_set:
            profile.tool_calling = await _probe_tool_calling(
                client, base_url, headers, model_id, profile.model_type, resolved_protocol
            )
        if "streaming" in check_set:
            profile.streaming = await _probe_streaming(
                client, base_url, headers, model_id, profile.model_type, resolved_protocol
            )
        if "structured_output" in check_set:
            structured_cap, json_mod = await _probe_structured_output(
                client, base_url, headers, model_id, profile.model_type, resolved_protocol
            )
            profile.structured_output = structured_cap
            if json_mod is not None:
                profile.output_modalities = _upsert_modality(profile.output_modalities, json_mod)
        if "input_modalities" in check_set:
            vision = await _probe_vision_input(
                client, base_url, headers, model_id, profile.model_type, resolved_protocol
            )
            profile.input_modalities = _upsert_modality(profile.input_modalities, vision)
        if "output_modalities" in check_set and "availability" not in check_set:
            # 仅选输出模态时复用基础可用性探测来观测文本/推理/图像/音频/向量输出。
            avail, observed_inputs, observed_outputs = await _probe_availability(
                client, base_url, headers, model_id, profile.model_type, resolved_protocol
            )
            profile.availability = avail
            for mod in observed_inputs:
                profile.input_modalities = _upsert_modality(profile.input_modalities, mod)
            for mod in observed_outputs:
                profile.output_modalities = _upsert_modality(profile.output_modalities, mod)
            if avail.status == "available" and profile.model_type == "unknown":
                has_reasoning = any(
                    mod.modality == "reasoning" and mod.status == "supported"
                    for mod in observed_outputs
                )
                profile.model_type = "reasoning" if has_reasoning else "chat"
                profile.type_source = "probed"
    except Exception as exc:
        detail = f"模型探测异常：{type(exc).__name__}: {exc}"
        if "availability" in check_set or "output_modalities" in check_set:
            profile.availability = CapabilityResult(status="error", source="probed", detail=detail)
        elif "tool_calling" in check_set:
            profile.tool_calling = CapabilityResult(status="error", source="probed", detail=detail)
        elif "streaming" in check_set:
            profile.streaming = CapabilityResult(status="error", source="probed", detail=detail)
        elif "structured_output" in check_set:
            profile.structured_output = CapabilityResult(
                status="error", source="probed", detail=detail
            )
    return profile


# 保留私有别名以兼容既有内部引用与测试。
_probe_single_model = probe_single_model


def _build_probe_response(
    base_url: str,
    resolved_protocol: ResolvedModelProtocol,
    results: list[ModelProfile],
    check_set: set[ProbeCheckName],
) -> ModelProbeResponse:
    """汇总全部模型的探测结果统计与可读摘要。"""
    available_count = sum(1 for item in results if item.availability.status == "available")
    tool_supported_count = sum(1 for item in results if item.tool_calling.status == "supported")
    streaming_supported_count = sum(1 for item in results if item.streaming.status == "supported")
    structured_output_supported_count = sum(
        1 for item in results if item.structured_output.status in {"supported", "partial"}
    )
    summary_parts = [
        f"已完成 {len(results)} 个模型探测：可用 {available_count} 个",
        f"支持工具调用 {tool_supported_count} 个",
    ]
    if "streaming" in check_set:
        summary_parts.append(f"流式可用 {streaming_supported_count} 个")
    if "structured_output" in check_set:
        summary_parts.append(f"结构化输出可用 {structured_output_supported_count} 个")
    return ModelProbeResponse(
        ok=True,
        protocol=resolved_protocol,
        base_url=base_url,
        tested_count=len(results),
        available_count=available_count,
        tool_supported_count=tool_supported_count,
        streaming_supported_count=streaming_supported_count,
        structured_output_supported_count=structured_output_supported_count,
        message="，".join(summary_parts),
        results=results,
    )


async def probe_models_stream(
    base_url: str,
    api_key: str | None,
    model_ids: list[str],
    checks: list[ProbeCheckName],
    *,
    protocol: ModelProtocol = "auto",
    timeout_seconds: float = 30.0,
    trust_env: bool = False,
    concurrency: int = 5,
    heartbeat_seconds: float = 10.0,
    initial_profiles: dict[str, ModelProfile] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """基于异步消费队列并发探测模型，跳过网络 IO 阻塞并在单模型完成时立即逐条推送结果。"""
    resolved_protocol = detect_protocol(base_url, protocol)
    check_set = set(checks)
    headers = build_protocol_headers(resolved_protocol, api_key)
    cleaned_ids = list(dict.fromkeys(mid.strip() for mid in model_ids if mid and mid.strip()))
    profile_map = initial_profiles or {}
    total = len(cleaned_ids)
    worker_count = min(max(1, concurrency), max(1, total))

    yield {
        "type": "start",
        "protocol": resolved_protocol,
        "base_url": base_url,
        "models": cleaned_ids,
        "total": total,
        "concurrency": worker_count,
    }

    if not cleaned_ids:
        empty_summary = _build_probe_response(base_url, resolved_protocol, [], check_set)
        yield {"type": "done", "result": empty_summary.model_dump(mode="json")}
        return

    task_queue: asyncio.Queue[str | None] = asyncio.Queue()
    for mid in cleaned_ids:
        task_queue.put_nowait(mid)
    for _ in range(worker_count):
        task_queue.put_nowait(None)

    event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    results_by_id: dict[str, ModelProfile] = {}
    completed = 0

    async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=trust_env) as client:

        async def consumer() -> None:
            nonlocal completed
            while True:
                model_id = await task_queue.get()
                if model_id is None:
                    task_queue.task_done()
                    return
                try:
                    await event_queue.put({"type": "model_start", "model_id": model_id})
                    profile = await probe_single_model(
                        client,
                        base_url,
                        headers,
                        model_id,
                        check_set,
                        resolved_protocol,
                        initial_profile=profile_map.get(model_id),
                    )
                    results_by_id[model_id] = profile
                    completed += 1
                    await event_queue.put(
                        {
                            "type": "model_result",
                            "model_id": model_id,
                            "model": profile.model_dump(mode="json"),
                            "completed": completed,
                            "total": total,
                        }
                    )
                finally:
                    task_queue.task_done()

        workers = [asyncio.create_task(consumer()) for _ in range(worker_count)]

        async def wait_consumers() -> None:
            try:
                await asyncio.gather(*workers)
            finally:
                await event_queue.put(None)

        coordinator = asyncio.create_task(wait_consumers())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                if event is None:
                    break
                yield event
            await coordinator
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            if not coordinator.done():
                coordinator.cancel()
            await asyncio.gather(*workers, coordinator, return_exceptions=True)

    ordered_results = [results_by_id[mid] for mid in cleaned_ids if mid in results_by_id]
    summary = _build_probe_response(base_url, resolved_protocol, ordered_results, check_set)
    yield {"type": "done", "result": summary.model_dump(mode="json")}


async def probe_models(
    base_url: str,
    api_key: str | None,
    model_ids: list[str],
    checks: list[ProbeCheckName],
    *,
    protocol: ModelProtocol = "auto",
    timeout_seconds: float = 30.0,
    trust_env: bool = False,
    concurrency: int = 5,
    initial_profiles: dict[str, ModelProfile] | None = None,
) -> ModelProbeResponse:
    """并发受控地探测所选模型的可用性、工具调用、流式响应、结构化输出与模态能力。"""
    final_result: ModelProbeResponse | None = None
    async for frame in probe_models_stream(
        base_url,
        api_key,
        model_ids,
        checks,
        protocol=protocol,
        timeout_seconds=timeout_seconds,
        trust_env=trust_env,
        concurrency=concurrency,
        initial_profiles=initial_profiles,
    ):
        if frame.get("type") == "done":
            final_result = ModelProbeResponse.model_validate(frame["result"])
    if final_result is not None:
        return final_result
    resolved_protocol = detect_protocol(base_url, protocol)
    return _build_probe_response(base_url, resolved_protocol, [], set(checks))
