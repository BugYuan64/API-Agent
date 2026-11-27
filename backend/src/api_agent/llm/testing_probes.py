"""专用端点与多协议（OpenAI 扩展、Anthropic、Gemini）模型探测实现。

本模块仅接收调用方已创建的 httpx.AsyncClient，不自行创建客户端，
便于上层统一控制超时、代理隔离与测试替换。
"""

import json
import struct
import time
from typing import Any
from urllib.parse import quote

import httpx

from api_agent.llm.testing_schemas import CapabilityResult, ModalityItem, ModelCategory

# 1x1 透明 PNG（68 字节），用于最小开销验证视觉输入模态是否被端点接受。
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
TINY_PNG_DATA_URI = f"data:image/png;base64,{TINY_PNG_BASE64}"

PROBE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
    "required": ["status"],
    "additionalProperties": False,
}

ANTHROPIC_TOOL_SPEC = [
    {
        "name": "probe_ping",
        "description": "Return a short echo value to verify tool calling support.",
        "input_schema": {
            "type": "object",
            "properties": {"echo": {"type": "string", "description": "Short token to echo"}},
            "required": ["echo"],
        },
    }
]

ANTHROPIC_STRUCTURED_TOOL_SPEC = [
    {
        "name": "emit_probe",
        "description": "Emit structured JSON status object.",
        "input_schema": PROBE_JSON_SCHEMA,
    }
]

GEMINI_TOOL_SPEC = [
    {
        "functionDeclarations": [
            {
                "name": "probe_ping",
                "description": "Return a short echo value to verify function calling support.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "echo": {"type": "STRING", "description": "Short token to echo"}
                    },
                    "required": ["echo"],
                },
            }
        ]
    }
]

GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"status": {"type": "STRING"}},
    "required": ["status"],
}

_MAX_SSE_LINES = 256
_MAX_SSE_BYTES = 65_536


def _build_tiny_wav_bytes() -> bytes:
    """构造 100ms 8kHz 单声道 16-bit PCM 静音 WAV，用于最小开销探测 ASR 端点。"""
    sample_rate = 8000
    num_samples = 800
    data_size = num_samples * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        data_size,
    )
    return header + (b"\x00" * data_size)


TINY_WAV_BYTES = _build_tiny_wav_bytes()


def is_asr_model(model_id: str) -> bool:
    """按常见语音识别命名区分 ASR（转写）与 TTS（语音合成）模型。"""
    lower = model_id.lower()
    return any(
        token in lower for token in ("whisper", "transcribe", "paraformer", "asr", "sensevoice")
    )


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    """从模型文本回复中提取 JSON 对象，兼容常见 Markdown 代码围栏。"""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def should_fallback_responses(resp: httpx.Response) -> bool:
    """判断服务端是否要求改用 OpenAI /responses（Responses API）端点。"""
    if resp.status_code not in {400, 404, 405, 422}:
        return False
    text = (resp.text or "").lower()
    return "chat_completions_disabled" in text or "/responses" in text


def extract_error_detail(resp: httpx.Response, limit: int = 140) -> str | None:
    """提取上游非 200 响应的简短错误原因，过滤换行并截断长度。"""
    candidate: str | None = None
    try:
        payload = resp.json()
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or err.get("type")
            if isinstance(msg, str) and msg.strip():
                candidate = msg
        elif isinstance(err, str) and err.strip():
            candidate = err
        if not candidate:
            for key in ("message", "detail"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    candidate = val
                    break
    if not candidate:
        raw = (resp.text or "").strip()
        if raw:
            candidate = raw
    if not candidate:
        return None
    compact = " ".join(candidate.split())
    if len(compact) > limit:
        return f"{compact[:limit]}…"
    return compact


# ==================== OpenAI 兼容专用端点探测 ====================


async def probe_rerank(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, list[ModalityItem], list[ModalityItem]]:
    """探测 POST /rerank 重排模型可用性与结果结构。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/rerank",
            headers=headers,
            json={
                "model": model_id,
                "query": "API testing",
                "documents": [
                    "API testing verifies HTTP status and JSON contracts.",
                    "Cooking pasta takes ten minutes.",
                ],
                "top_n": 2,
            },
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="Rerank 探测超时",
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
    if resp.status_code in {401, 403}:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"鉴权失败（HTTP {resp.status_code}）",
            ),
            [],
            [],
        )
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"Rerank 端点返回 HTTP {resp.status_code}",
            ),
            [],
            [],
        )
    try:
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            results = body.get("data") if isinstance(body, dict) else None
        if not isinstance(results, list) or not results:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Rerank 响应缺少 results/data 排序数组",
                ),
                [],
                [],
            )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail="Rerank 返回结构非法",
            ),
            [],
            [],
        )
    return (
        CapabilityResult(
            status="available",
            source="probed",
            latency_ms=elapsed,
            detail="Rerank 端点响应正常（/rerank）",
        ),
        [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本重排输入"
            )
        ],
        [
            ModalityItem(
                modality="json", status="supported", source="probed", detail="实测返回重排分数结果"
            )
        ],
    )


async def probe_moderation(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, list[ModalityItem], list[ModalityItem]]:
    """探测 POST /moderations 安全审核端点可用性。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/moderations",
            headers=headers,
            json={"model": model_id, "input": "hello world"},
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="Moderations 探测超时",
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
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"Moderations 端点返回 HTTP {resp.status_code}",
            ),
            [],
            [],
        )
    try:
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or not results:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Moderations 响应缺少 results 数组",
                ),
                [],
                [],
            )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail="Moderations 返回结构非法",
            ),
            [],
            [],
        )
    return (
        CapabilityResult(
            status="available",
            source="probed",
            latency_ms=elapsed,
            detail="Moderations 端点响应正常（/moderations）",
        ),
        [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本审核输入"
            )
        ],
        [
            ModalityItem(
                modality="json", status="supported", source="probed", detail="实测返回审核分类结果"
            )
        ],
    )


async def probe_image_generation(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, list[ModalityItem], list[ModalityItem]]:
    """探测 POST /images/generations 文生图端点（默认 256x256，尺寸不兼容时省略尺寸重试一次）。"""
    started = time.monotonic()
    payload: dict[str, Any] = {
        "model": model_id,
        "prompt": "a solid blue dot",
        "size": "256x256",
        "n": 1,
    }
    try:
        resp = await client.post(
            f"{base_url}/images/generations",
            headers=headers,
            json=payload,
        )
        if resp.status_code in {400, 422} and "size" in (resp.text or "").lower():
            retry_payload = {k: v for k, v in payload.items() if k != "size"}
            resp = await client.post(
                f"{base_url}/images/generations",
                headers=headers,
                json=retry_payload,
            )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="图像生成探测超时",
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
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"Images 端点返回 HTTP {resp.status_code}",
            ),
            [],
            [],
        )
    try:
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        has_image = isinstance(data, list) and any(
            isinstance(item, dict) and (item.get("url") or item.get("b64_json")) for item in data
        )
        if not has_image:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Images 响应缺少有效 url/b64_json",
                ),
                [],
                [],
            )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail="Images 返回结构非法",
            ),
            [],
            [],
        )
    return (
        CapabilityResult(
            status="available",
            source="probed",
            latency_ms=elapsed,
            detail="Images 端点响应正常（/images/generations）",
        ),
        [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本提示输入"
            )
        ],
        [
            ModalityItem(
                modality="image", status="supported", source="probed", detail="实测返回生成图像"
            )
        ],
    )


async def probe_audio(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, list[ModalityItem], list[ModalityItem]]:
    """按模型特征探测音频转写（/audio/transcriptions）或语音合成（/audio/speech）。"""
    started = time.monotonic()
    if is_asr_model(model_id):
        multipart_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        try:
            resp = await client.post(
                f"{base_url}/audio/transcriptions",
                headers=multipart_headers,
                data={"model": model_id},
                files={"file": ("probe.wav", TINY_WAV_BYTES, "audio/wav")},
            )
        except httpx.TimeoutException:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    detail="语音识别探测超时",
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
        if resp.status_code != 200:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=elapsed,
                    detail=f"Audio Transcriptions 端点返回 HTTP {resp.status_code}",
                ),
                [],
                [],
            )
        try:
            body = resp.json()
            if not isinstance(body, dict) or "text" not in body:
                return (
                    CapabilityResult(
                        status="unavailable",
                        source="probed",
                        latency_ms=elapsed,
                        detail="语音识别响应缺少 text 字段",
                    ),
                    [],
                    [],
                )
        except (ValueError, TypeError):
            if not (resp.text or "").strip():
                return (
                    CapabilityResult(
                        status="unavailable",
                        source="probed",
                        latency_ms=elapsed,
                        detail="语音识别响应为空",
                    ),
                    [],
                    [],
                )
        return (
            CapabilityResult(
                status="available",
                source="probed",
                latency_ms=elapsed,
                detail="Audio Transcriptions 端点响应正常（/audio/transcriptions）",
            ),
            [
                ModalityItem(
                    modality="audio",
                    status="supported",
                    source="probed",
                    detail="实测接受 WAV 音频输入",
                )
            ],
            [
                ModalityItem(
                    modality="text",
                    status="supported",
                    source="probed",
                    detail="实测返回转写文本",
                )
            ],
        )

    # TTS：POST /audio/speech
    try:
        resp = await client.post(
            f"{base_url}/audio/speech",
            headers=headers,
            json={"model": model_id, "input": "OK", "voice": "alloy", "response_format": "mp3"},
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="语音合成探测超时",
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
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"Audio Speech 端点返回 HTTP {resp.status_code}",
            ),
            [],
            [],
        )
    content_type = (resp.headers.get("content-type") or "").lower()
    if not resp.content or "application/json" in content_type:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail="Audio Speech 未返回音频二进制流",
            ),
            [],
            [],
        )
    return (
        CapabilityResult(
            status="available",
            source="probed",
            latency_ms=elapsed,
            detail=f"Audio Speech 端点响应正常（/audio/speech，{len(resp.content)} B）",
        ),
        [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本合成输入"
            )
        ],
        [
            ModalityItem(
                modality="audio", status="supported", source="probed", detail="实测返回音频流"
            )
        ],
    )


# ==================== OpenAI 进阶能力：SSE 流式与结构化输出 ====================


async def probe_openai_streaming(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
) -> CapabilityResult:
    """探测 OpenAI /chat/completions 流式 SSE（含首字延迟 TTFT），自动回退 /responses。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return CapabilityResult(
            status="unsupported", source="inferred", detail="非对话模型不适用流式文本探测"
        )
    started = time.monotonic()
    fallback_needed = False
    try:
        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={**headers, "Accept": "text/event-stream"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "max_tokens": 64,
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                if should_fallback_responses(resp):
                    fallback_needed = True
                else:
                    elapsed = round((time.monotonic() - started) * 1000, 2)
                    status = "unsupported" if resp.status_code in {400, 404, 422, 501} else "error"
                    err_hint = extract_error_detail(resp)
                    detail = f"流式端点返回 HTTP {resp.status_code}"
                    if err_hint:
                        detail = f"{detail} · {err_hint}"
                    return CapabilityResult(
                        status=status,
                        source="probed",
                        latency_ms=elapsed,
                        detail=detail,
                    )
            else:
                content_type = (resp.headers.get("content-type") or "").lower()
                if "application/json" in content_type and "text/event-stream" not in content_type:
                    await resp.aread()
                    elapsed = round((time.monotonic() - started) * 1000, 2)
                    return CapabilityResult(
                        status="partial",
                        source="probed",
                        latency_ms=elapsed,
                        detail="服务端返回普通 JSON 而非 SSE 事件流",
                    )
                ttft_ms: float | None = None
                chunks = 0
                total_bytes = 0
                lines_read = 0
                async for line in resp.aiter_lines():
                    lines_read += 1
                    total_bytes += len(line.encode("utf-8", errors="ignore"))
                    if lines_read > _MAX_SSE_LINES or total_bytes > _MAX_SSE_BYTES:
                        break
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    if data_str == "[DONE]":
                        break
                    chunks += 1
                    if ttft_ms is None:
                        try:
                            event = json.loads(data_str)
                            choices = event.get("choices") if isinstance(event, dict) else None
                            delta = (
                                choices[0].get("delta")
                                if isinstance(choices, list)
                                and choices
                                and isinstance(choices[0], dict)
                                else None
                            )
                            if isinstance(delta, dict) and (
                                delta.get("content")
                                or delta.get("reasoning_content")
                                or delta.get("reasoning")
                            ):
                                ttft_ms = round((time.monotonic() - started) * 1000, 2)
                        except (ValueError, TypeError, AttributeError):
                            pass
                elapsed = round((time.monotonic() - started) * 1000, 2)
                if chunks == 0:
                    return CapabilityResult(
                        status="partial",
                        source="probed",
                        latency_ms=elapsed,
                        detail="SSE 连接已建立但未收到有效数据分片",
                    )
                effective_ttft = ttft_ms if ttft_ms is not None else elapsed
                return CapabilityResult(
                    status="supported",
                    source="probed",
                    latency_ms=elapsed,
                    ttft_ms=effective_ttft,
                    detail=f"SSE 流式可用（首字 {effective_ttft:.0f}ms，收到 {chunks} 块）",
                )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="流式探测超时",
        )
    except (httpx.HTTPError, OSError):
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="连接失败",
        )

    if fallback_needed:
        return await probe_openai_streaming_responses(client, base_url, headers, model_id)
    return CapabilityResult(status="error", source="probed", detail="流式探测未取得响应")


async def probe_openai_streaming_responses(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> CapabilityResult:
    """通过 OpenAI /responses 端点探测 SSE 流式输出与 TTFT。"""
    started = time.monotonic()
    try:
        async with client.stream(
            "POST",
            f"{base_url}/responses",
            headers={**headers, "Accept": "text/event-stream"},
            json={
                "model": model_id,
                "input": "Reply with OK",
                "max_output_tokens": 64,
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                elapsed = round((time.monotonic() - started) * 1000, 2)
                status = "unsupported" if resp.status_code in {400, 404, 422, 501} else "error"
                err_hint = extract_error_detail(resp)
                detail = f"Responses 流式返回 HTTP {resp.status_code}"
                if err_hint:
                    detail = f"{detail} · {err_hint}"
                return CapabilityResult(
                    status=status,
                    source="probed",
                    latency_ms=elapsed,
                    detail=detail,
                )
            ttft_ms: float | None = None
            chunks = 0
            total_bytes = 0
            lines_read = 0
            async for line in resp.aiter_lines():
                lines_read += 1
                total_bytes += len(line.encode("utf-8", errors="ignore"))
                if lines_read > _MAX_SSE_LINES or total_bytes > _MAX_SSE_BYTES:
                    break
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    if data_str == "[DONE]":
                        break
                    continue
                chunks += 1
                if ttft_ms is None:
                    try:
                        event = json.loads(data_str)
                        if isinstance(event, dict) and (
                            event.get("delta")
                            or event.get("type")
                            in {
                                "response.output_text.delta",
                                "response.reasoning_summary_text.delta",
                            }
                        ):
                            ttft_ms = round((time.monotonic() - started) * 1000, 2)
                    except (ValueError, TypeError):
                        pass
            elapsed = round((time.monotonic() - started) * 1000, 2)
            if chunks == 0:
                return CapabilityResult(
                    status="partial",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Responses SSE 未收到有效分片",
                )
            effective_ttft = ttft_ms if ttft_ms is not None else elapsed
            return CapabilityResult(
                status="supported",
                source="probed",
                latency_ms=elapsed,
                ttft_ms=effective_ttft,
                detail=f"Responses SSE 流式可用（首字 {effective_ttft:.0f}ms，收到 {chunks} 块）",
            )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="Responses 流式探测超时",
        )
    except (httpx.HTTPError, OSError):
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="连接失败",
        )


async def probe_openai_structured_output(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
) -> tuple[CapabilityResult, ModalityItem | None]:
    """探测结构化输出：优先验证 json_schema，不支持时降级验证 json_object，自动兼容 /responses。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return (
            CapabilityResult(
                status="unsupported", source="inferred", detail="非对话模型不适用结构化输出探测"
            ),
            None,
        )

    started = time.monotonic()
    schema_payload = {
        "model": model_id,
        "messages": [
            {"role": "user", "content": 'Return a JSON object strictly matching {"status":"ok"}.'}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "probe_schema",
                "strict": True,
                "schema": PROBE_JSON_SCHEMA,
            },
        },
        "max_tokens": 64,
        "stream": False,
    }
    try:
        resp = await client.post(
            f"{base_url}/chat/completions", headers=headers, json=schema_payload
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="结构化输出探测超时",
            ),
            None,
        )
    except (httpx.HTTPError, OSError):
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="连接失败",
            ),
            None,
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if should_fallback_responses(resp):
        return await probe_openai_structured_output_responses(client, base_url, headers, model_id)

    if resp.status_code == 200:
        try:
            body = resp.json()
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
            parsed = extract_json_object(content)
            if parsed is not None and isinstance(parsed.get("status"), str):
                return (
                    CapabilityResult(
                        status="supported",
                        source="probed",
                        latency_ms=elapsed,
                        detail="实测支持 json_schema 严格结构化输出",
                    ),
                    ModalityItem(
                        modality="json",
                        status="supported",
                        source="probed",
                        detail="实测支持 json_schema 结构化输出",
                    ),
                )
            return (
                CapabilityResult(
                    status="partial",
                    source="probed",
                    latency_ms=elapsed,
                    detail="接受 json_schema 但输出未严格匹配 Schema",
                ),
                None,
            )
        except (ValueError, TypeError, IndexError, AttributeError):
            return (
                CapabilityResult(
                    status="error", source="probed", latency_ms=elapsed, detail="响应解析失败"
                ),
                None,
            )

    if resp.status_code not in {400, 404, 422, 501}:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=elapsed,
                detail=f"HTTP {resp.status_code}",
            ),
            None,
        )

    # 回退验证 json_object 模式
    fallback_started = time.monotonic()
    try:
        fb_resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": 'Return JSON {"status":"ok"}.'}],
                "response_format": {"type": "json_object"},
                "max_tokens": 64,
                "temperature": 0,
                "stream": False,
            },
        )
    except (httpx.HTTPError, OSError):
        return (
            CapabilityResult(
                status="unsupported",
                source="probed",
                latency_ms=elapsed,
                detail=f"服务端拒绝 json_schema（HTTP {resp.status_code}）",
            ),
            None,
        )

    fb_elapsed = round((time.monotonic() - fallback_started) * 1000, 2)
    if fb_resp.status_code == 200:
        try:
            body = fb_resp.json()
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
            if extract_json_object(content) is not None:
                return (
                    CapabilityResult(
                        status="partial",
                        source="probed",
                        latency_ms=fb_elapsed,
                        detail="不支持 json_schema，实测支持 json_object",
                    ),
                    ModalityItem(
                        modality="json",
                        status="supported",
                        source="probed",
                        detail="实测支持 json_object 结构化输出",
                    ),
                )
        except (ValueError, TypeError, IndexError, AttributeError):
            pass

    return (
        CapabilityResult(
            status="unsupported",
            source="probed",
            latency_ms=fb_elapsed,
            detail=f"不支持 json_schema 与 json_object（HTTP {fb_resp.status_code}）",
        ),
        None,
    )


async def probe_openai_structured_output_responses(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, ModalityItem | None]:
    """通过 /responses 的 text.format 探测 json_schema 结构化输出。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/responses",
            headers=headers,
            json={
                "model": model_id,
                "input": 'Return JSON matching {"status":"ok"}.',
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "probe_schema",
                        "strict": True,
                        "schema": PROBE_JSON_SCHEMA,
                    }
                },
                "max_output_tokens": 64,
            },
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="Responses 结构化输出探测超时",
            ),
            None,
        )
    except (httpx.HTTPError, OSError):
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="连接失败",
            ),
            None,
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if resp.status_code in {400, 404, 422, 501}:
        return (
            CapabilityResult(
                status="unsupported",
                source="probed",
                latency_ms=elapsed,
                detail=f"Responses 端点拒绝 text.format（HTTP {resp.status_code}）",
            ),
            None,
        )
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=elapsed,
                detail=f"HTTP {resp.status_code}",
            ),
            None,
        )
    try:
        body = resp.json()
        text = body.get("output_text")
        if not text and isinstance(body.get("output"), list):
            for item in body["output"]:
                if isinstance(item, dict) and item.get("type") == "message":
                    content = item.get("content")
                    if isinstance(content, str):
                        text = content
                        break
                    if isinstance(content, list):
                        text = "".join(
                            part.get("text", "")
                            for part in content
                            if isinstance(part, dict) and isinstance(part.get("text"), str)
                        )
                        if text:
                            break
        parsed = extract_json_object(text)
        if parsed is not None and isinstance(parsed.get("status"), str):
            return (
                CapabilityResult(
                    status="supported",
                    source="probed",
                    latency_ms=elapsed,
                    detail="实测支持 json_schema 结构化输出（/responses）",
                ),
                ModalityItem(
                    modality="json",
                    status="supported",
                    source="probed",
                    detail="实测支持结构化 JSON 输出（/responses）",
                ),
            )
        return (
            CapabilityResult(
                status="partial",
                source="probed",
                latency_ms=elapsed,
                detail="Responses 端点接受 text.format 但输出未严格匹配",
            ),
            None,
        )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="error", source="probed", latency_ms=elapsed, detail="响应解析失败"
            ),
            None,
        )


# ==================== Anthropic Messages API 探测 ====================


async def probe_anthropic_availability(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, list[ModalityItem]]:
    """探测 Anthropic POST /messages 基础可用性。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/messages",
            headers=headers,
            json={
                "model": model_id,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with OK"}],
            },
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="Anthropic /messages 探测超时",
            ),
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
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if resp.status_code in {401, 403}:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"鉴权失败（HTTP {resp.status_code}）",
            ),
            [],
        )
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"Anthropic /messages 返回 HTTP {resp.status_code}",
            ),
            [],
        )
    try:
        body = resp.json()
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, list) or not content:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Anthropic 响应缺少 content 数组",
                ),
                [],
            )
        out_mods = [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本回复"
            )
        ]
        if any(isinstance(block, dict) and block.get("type") == "thinking" for block in content):
            out_mods.append(
                ModalityItem(
                    modality="reasoning",
                    status="supported",
                    source="probed",
                    detail="实测返回 thinking 推理块",
                )
            )
        return (
            CapabilityResult(
                status="available",
                source="probed",
                latency_ms=elapsed,
                detail="Anthropic Messages 可用（/messages）",
            ),
            out_mods,
        )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="unavailable", source="probed", latency_ms=elapsed, detail="返回结构非法"
            ),
            [],
        )


async def probe_anthropic_tool_calling(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> CapabilityResult:
    """探测 Anthropic Messages API 工具调用（tool_use）。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/messages",
            headers=headers,
            json={
                "model": model_id,
                "max_tokens": 96,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "You must call the probe_ping tool with echo='ok'. "
                            "Do not answer directly."
                        ),
                    }
                ],
                "tools": ANTHROPIC_TOOL_SPEC,
                "tool_choice": {"type": "auto"},
            },
        )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="Anthropic 工具探测超时",
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
            detail=f"Anthropic 端点拒绝 tools 参数（HTTP {resp.status_code}）",
        )
    if resp.status_code != 200:
        return CapabilityResult(
            status="error", source="probed", latency_ms=elapsed, detail=f"HTTP {resp.status_code}"
        )
    try:
        content = resp.json().get("content") or []
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in content
        ):
            return CapabilityResult(
                status="supported",
                source="probed",
                latency_ms=elapsed,
                detail="实测返回有效 tool_use（Anthropic）",
            )
        return CapabilityResult(
            status="partial",
            source="probed",
            latency_ms=elapsed,
            detail="接受 tools 参数但本次未触发 tool_use",
        )
    except (ValueError, TypeError, AttributeError):
        return CapabilityResult(
            status="error", source="probed", latency_ms=elapsed, detail="工具响应解析失败"
        )


async def probe_anthropic_vision(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> ModalityItem:
    """用 1x1 PNG base64 块探测 Anthropic 视觉输入支持。"""
    try:
        resp = await client.post(
            f"{base_url}/messages",
            headers=headers,
            json={
                "model": model_id,
                "max_tokens": 16,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": TINY_PNG_BASE64,
                                },
                            },
                            {"type": "text", "text": "Reply OK if you received the image."},
                        ],
                    }
                ],
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
            detail="实测接受 base64 image 输入（Anthropic）",
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


async def probe_anthropic_streaming(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> CapabilityResult:
    """探测 Anthropic /messages SSE 流式输出与首字延迟 TTFT。"""
    started = time.monotonic()
    try:
        async with client.stream(
            "POST",
            f"{base_url}/messages",
            headers={**headers, "Accept": "text/event-stream"},
            json={
                "model": model_id,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                elapsed = round((time.monotonic() - started) * 1000, 2)
                status = "unsupported" if resp.status_code in {400, 404, 422, 501} else "error"
                return CapabilityResult(
                    status=status,
                    source="probed",
                    latency_ms=elapsed,
                    detail=f"Anthropic 流式返回 HTTP {resp.status_code}",
                )
            ttft_ms: float | None = None
            chunks = 0
            total_bytes = 0
            lines_read = 0
            async for line in resp.aiter_lines():
                lines_read += 1
                total_bytes += len(line.encode("utf-8", errors="ignore"))
                if lines_read > _MAX_SSE_LINES or total_bytes > _MAX_SSE_BYTES:
                    break
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                chunks += 1
                if ttft_ms is None:
                    try:
                        event = json.loads(data_str)
                        if isinstance(event, dict) and event.get("type") == "content_block_delta":
                            ttft_ms = round((time.monotonic() - started) * 1000, 2)
                    except (ValueError, TypeError):
                        pass
            elapsed = round((time.monotonic() - started) * 1000, 2)
            if chunks == 0:
                return CapabilityResult(
                    status="partial",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Anthropic SSE 未收到有效分片",
                )
            effective_ttft = ttft_ms if ttft_ms is not None else elapsed
            return CapabilityResult(
                status="supported",
                source="probed",
                latency_ms=elapsed,
                ttft_ms=effective_ttft,
                detail=f"Anthropic SSE 可用（首字 {effective_ttft:.0f}ms，收到 {chunks} 块）",
            )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="Anthropic 流式探测超时",
        )
    except (httpx.HTTPError, OSError):
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="连接失败",
        )


async def probe_anthropic_structured_output(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
) -> tuple[CapabilityResult, ModalityItem | None]:
    """通过强制工具 Schema 探测 Anthropic 结构化 JSON 输出能力。"""
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/messages",
            headers=headers,
            json={
                "model": model_id,
                "max_tokens": 96,
                "messages": [{"role": "user", "content": "Emit status='ok'."}],
                "tools": ANTHROPIC_STRUCTURED_TOOL_SPEC,
                "tool_choice": {"type": "tool", "name": "emit_probe"},
            },
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="Anthropic 结构化输出探测超时",
            ),
            None,
        )
    except (httpx.HTTPError, OSError):
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="连接失败",
            ),
            None,
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if resp.status_code in {400, 404, 422, 501}:
        return (
            CapabilityResult(
                status="unsupported",
                source="probed",
                latency_ms=elapsed,
                detail=f"Anthropic 端点拒绝强制 Schema 工具（HTTP {resp.status_code}）",
            ),
            None,
        )
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=elapsed,
                detail=f"HTTP {resp.status_code}",
            ),
            None,
        )
    try:
        content = resp.json().get("content") or []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and isinstance(block.get("input"), dict)
                and isinstance(block["input"].get("status"), str)
            ):
                return (
                    CapabilityResult(
                        status="supported",
                        source="probed",
                        latency_ms=elapsed,
                        detail="实测支持工具 Schema 约束 JSON 输出（Anthropic）",
                    ),
                    ModalityItem(
                        modality="json",
                        status="supported",
                        source="probed",
                        detail="实测支持结构化 JSON 输出（Anthropic）",
                    ),
                )
        return (
            CapabilityResult(
                status="partial",
                source="probed",
                latency_ms=elapsed,
                detail="端点接受 Schema 工具但未返回匹配对象",
            ),
            None,
        )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="error", source="probed", latency_ms=elapsed, detail="响应解析失败"
            ),
            None,
        )


# ==================== Gemini generateContent API 探测 ====================


def gemini_model_url(base_url: str, model_id: str, action: str) -> str:
    """构造 Gemini 模型动作 URL（自动兼容是否带 models/ 前缀）。"""
    clean_id = model_id.removeprefix("models/")
    encoded_id = quote(clean_id, safe="")
    return f"{base_url}/models/{encoded_id}:{action}"


async def probe_gemini_availability(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
) -> tuple[CapabilityResult, list[ModalityItem]]:
    """探测 Gemini :generateContent 或 :embedContent 基础可用性。"""
    started = time.monotonic()
    if category == "embedding":
        try:
            resp = await client.post(
                gemini_model_url(base_url, model_id, "embedContent"),
                headers=headers,
                json={"content": {"parts": [{"text": "ping"}]}},
            )
        except httpx.TimeoutException:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    detail="Gemini embedContent 探测超时",
                ),
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
            )
        elapsed = round((time.monotonic() - started) * 1000, 2)
        if resp.status_code == 200:
            try:
                body = resp.json()
                values = (
                    (body.get("embedding") or {}).get("values") if isinstance(body, dict) else None
                )
                if isinstance(values, list) and values:
                    return (
                        CapabilityResult(
                            status="available",
                            source="probed",
                            latency_ms=elapsed,
                            detail="Gemini embedContent 可用",
                        ),
                        [
                            ModalityItem(
                                modality="embedding",
                                status="supported",
                                source="probed",
                                detail="实测返回向量响应（Gemini）",
                            )
                        ],
                    )
            except (ValueError, TypeError, AttributeError):
                pass
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"Gemini embedContent 返回 HTTP {resp.status_code}",
            ),
            [],
        )

    try:
        resp = await client.post(
            gemini_model_url(base_url, model_id, "generateContent"),
            headers=headers,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Reply with OK"}]}],
                "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
            },
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="Gemini generateContent 探测超时",
            ),
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
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if resp.status_code in {401, 403}:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"鉴权失败（HTTP {resp.status_code}）",
            ),
            [],
        )
    if resp.status_code != 200:
        return (
            CapabilityResult(
                status="unavailable",
                source="probed",
                latency_ms=elapsed,
                detail=f"Gemini generateContent 返回 HTTP {resp.status_code}",
            ),
            [],
        )
    try:
        body = resp.json()
        candidates = body.get("candidates") if isinstance(body, dict) else None
        if not isinstance(candidates, list) or not candidates:
            return (
                CapabilityResult(
                    status="unavailable",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Gemini 响应缺少 candidates",
                ),
                [],
            )
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        out_mods = [
            ModalityItem(
                modality="text", status="supported", source="probed", detail="实测支持文本回复"
            )
        ]
        if any(isinstance(part, dict) and part.get("thought") for part in parts):
            out_mods.append(
                ModalityItem(
                    modality="reasoning",
                    status="supported",
                    source="probed",
                    detail="实测返回 thought 推理片段（Gemini）",
                )
            )
        return (
            CapabilityResult(
                status="available",
                source="probed",
                latency_ms=elapsed,
                detail="Gemini generateContent 可用",
            ),
            out_mods,
        )
    except (ValueError, TypeError, AttributeError):
        return (
            CapabilityResult(
                status="unavailable", source="probed", latency_ms=elapsed, detail="返回结构非法"
            ),
            [],
        )


async def probe_gemini_tool_calling(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
) -> CapabilityResult:
    """探测 Gemini functionDeclarations 函数调用支持。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return CapabilityResult(
            status="unsupported", source="inferred", detail="非对话模型不适用 Function Calling"
        )
    started = time.monotonic()
    try:
        resp = await client.post(
            gemini_model_url(base_url, model_id, "generateContent"),
            headers=headers,
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": (
                                    "You must call the probe_ping tool with echo='ok'. "
                                    "Do not answer directly."
                                )
                            }
                        ],
                    }
                ],
                "tools": GEMINI_TOOL_SPEC,
                "generationConfig": {"maxOutputTokens": 64, "temperature": 0},
            },
        )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="Gemini 工具探测超时",
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
            detail=f"Gemini 端点拒绝 tools 参数（HTTP {resp.status_code}）",
        )
    if resp.status_code != 200:
        return CapabilityResult(
            status="error", source="probed", latency_ms=elapsed, detail=f"HTTP {resp.status_code}"
        )
    try:
        candidates = resp.json().get("candidates") or []
        parts = (
            ((candidates[0] or {}).get("content") or {}).get("parts") or [] if candidates else []
        )
        if any(isinstance(part, dict) and part.get("functionCall") for part in parts):
            return CapabilityResult(
                status="supported",
                source="probed",
                latency_ms=elapsed,
                detail="实测返回有效 functionCall（Gemini）",
            )
        return CapabilityResult(
            status="partial",
            source="probed",
            latency_ms=elapsed,
            detail="接受 tools 参数但本次未触发 functionCall",
        )
    except (ValueError, TypeError, AttributeError):
        return CapabilityResult(
            status="error", source="probed", latency_ms=elapsed, detail="工具响应解析失败"
        )


async def probe_gemini_vision(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
) -> ModalityItem:
    """用 1x1 PNG inlineData 探测 Gemini 图像输入能力。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return ModalityItem(
            modality="image", status="unsupported", source="inferred", detail="非对话模型"
        )
    try:
        resp = await client.post(
            gemini_model_url(base_url, model_id, "generateContent"),
            headers=headers,
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "Reply OK if you received the image."},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": TINY_PNG_BASE64,
                                }
                            },
                        ],
                    }
                ],
                "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
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
            detail="实测接受 inlineData 图像输入（Gemini）",
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


async def probe_gemini_streaming(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
) -> CapabilityResult:
    """探测 Gemini :streamGenerateContent?alt=sse 流式输出与首字延迟 TTFT。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return CapabilityResult(
            status="unsupported", source="inferred", detail="非对话模型不适用流式文本探测"
        )
    started = time.monotonic()
    try:
        async with client.stream(
            "POST",
            f"{gemini_model_url(base_url, model_id, 'streamGenerateContent')}?alt=sse",
            headers={**headers, "Accept": "text/event-stream"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Reply with OK"}]}],
                "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
            },
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                elapsed = round((time.monotonic() - started) * 1000, 2)
                status = "unsupported" if resp.status_code in {400, 404, 422, 501} else "error"
                return CapabilityResult(
                    status=status,
                    source="probed",
                    latency_ms=elapsed,
                    detail=f"Gemini 流式返回 HTTP {resp.status_code}",
                )
            ttft_ms: float | None = None
            chunks = 0
            total_bytes = 0
            lines_read = 0
            async for line in resp.aiter_lines():
                lines_read += 1
                total_bytes += len(line.encode("utf-8", errors="ignore"))
                if lines_read > _MAX_SSE_LINES or total_bytes > _MAX_SSE_BYTES:
                    break
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                chunks += 1
                if ttft_ms is None:
                    try:
                        event = json.loads(data_str)
                        candidates = event.get("candidates") if isinstance(event, dict) else None
                        if isinstance(candidates, list) and candidates:
                            ttft_ms = round((time.monotonic() - started) * 1000, 2)
                    except (ValueError, TypeError):
                        pass
            elapsed = round((time.monotonic() - started) * 1000, 2)
            if chunks == 0:
                return CapabilityResult(
                    status="partial",
                    source="probed",
                    latency_ms=elapsed,
                    detail="Gemini SSE 未收到有效分片",
                )
            effective_ttft = ttft_ms if ttft_ms is not None else elapsed
            return CapabilityResult(
                status="supported",
                source="probed",
                latency_ms=elapsed,
                ttft_ms=effective_ttft,
                detail=f"Gemini SSE 可用（首字 {effective_ttft:.0f}ms，收到 {chunks} 块）",
            )
    except httpx.TimeoutException:
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="Gemini 流式探测超时",
        )
    except (httpx.HTTPError, OSError):
        return CapabilityResult(
            status="error",
            source="probed",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            detail="连接失败",
        )


async def probe_gemini_structured_output(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    model_id: str,
    category: ModelCategory,
) -> tuple[CapabilityResult, ModalityItem | None]:
    """探测 Gemini responseMimeType + responseSchema 结构化输出。"""
    if category not in {"chat", "reasoning", "unknown"}:
        return (
            CapabilityResult(
                status="unsupported", source="inferred", detail="非对话模型不适用结构化输出探测"
            ),
            None,
        )
    started = time.monotonic()
    url = gemini_model_url(base_url, model_id, "generateContent")
    try:
        resp = await client.post(
            url,
            headers=headers,
            json={
                "contents": [{"role": "user", "parts": [{"text": 'Return JSON {"status":"ok"}.'}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": GEMINI_RESPONSE_SCHEMA,
                    "maxOutputTokens": 64,
                    "temperature": 0,
                },
            },
        )
    except httpx.TimeoutException:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="Gemini 结构化输出探测超时",
            ),
            None,
        )
    except (httpx.HTTPError, OSError):
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                detail="连接失败",
            ),
            None,
        )

    elapsed = round((time.monotonic() - started) * 1000, 2)
    if resp.status_code == 200:
        try:
            candidates = resp.json().get("candidates") or []
            parts = (
                ((candidates[0] or {}).get("content") or {}).get("parts") or []
                if candidates
                else []
            )
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and part.get("text")
            )
            parsed = extract_json_object(text)
            if parsed is not None and isinstance(parsed.get("status"), str):
                return (
                    CapabilityResult(
                        status="supported",
                        source="probed",
                        latency_ms=elapsed,
                        detail="实测支持 responseSchema 严格 JSON 输出（Gemini）",
                    ),
                    ModalityItem(
                        modality="json",
                        status="supported",
                        source="probed",
                        detail="实测支持 responseSchema 结构化输出（Gemini）",
                    ),
                )
            return (
                CapabilityResult(
                    status="partial",
                    source="probed",
                    latency_ms=elapsed,
                    detail="接受 responseSchema 但输出未严格匹配",
                ),
                None,
            )
        except (ValueError, TypeError, AttributeError):
            return (
                CapabilityResult(
                    status="error", source="probed", latency_ms=elapsed, detail="响应解析失败"
                ),
                None,
            )

    if resp.status_code not in {400, 404, 422, 501}:
        return (
            CapabilityResult(
                status="error",
                source="probed",
                latency_ms=elapsed,
                detail=f"HTTP {resp.status_code}",
            ),
            None,
        )

    # 回退仅验证 responseMimeType: application/json
    fb_started = time.monotonic()
    try:
        fb_resp = await client.post(
            url,
            headers=headers,
            json={
                "contents": [{"role": "user", "parts": [{"text": 'Return JSON {"status":"ok"}.'}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 64,
                    "temperature": 0,
                },
            },
        )
    except (httpx.HTTPError, OSError):
        return (
            CapabilityResult(
                status="unsupported",
                source="probed",
                latency_ms=elapsed,
                detail=f"Gemini 拒绝 responseSchema（HTTP {resp.status_code}）",
            ),
            None,
        )

    fb_elapsed = round((time.monotonic() - fb_started) * 1000, 2)
    if fb_resp.status_code == 200:
        try:
            candidates = fb_resp.json().get("candidates") or []
            parts = (
                ((candidates[0] or {}).get("content") or {}).get("parts") or []
                if candidates
                else []
            )
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and part.get("text")
            )
            if extract_json_object(text) is not None:
                return (
                    CapabilityResult(
                        status="partial",
                        source="probed",
                        latency_ms=fb_elapsed,
                        detail="不支持 responseSchema，实测支持 JSON MIME 输出（Gemini）",
                    ),
                    ModalityItem(
                        modality="json",
                        status="supported",
                        source="probed",
                        detail="实测支持 JSON MIME 输出（Gemini）",
                    ),
                )
        except (ValueError, TypeError, AttributeError):
            pass

    return (
        CapabilityResult(
            status="unsupported",
            source="probed",
            latency_ms=fb_elapsed,
            detail=f"Gemini 不支持结构化输出（HTTP {fb_resp.status_code}）",
        ),
        None,
    )
