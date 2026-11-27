/**
 * 构造发送给 AI 助手小Y 的当前测试页实时数据快照，
 * 在浏览器侧先行脱敏敏感请求头、URL 凭据并截断超大响应体，配合后端二次清洗确保零凭据泄漏。
 */
import type { AssistantCurrentTestContext, TaskRecord } from '@/api/tasks'
import type {
  ConsoleResult,
  ConsoleSseEvent,
  ModelProfile,
  ModelProtocol,
  StreamEvent,
  StreamSession,
} from '@/api/types'
import type { ResultPresentation } from './results'

const SENSITIVE_HEADER_RE = /(authorization|cookie|token|secret|password|passwd|api[-_]?key|session|credential)/i
const SENSITIVE_QUERY_KEY_RE = /^(api[-_]?key|key|token|access[-_]?token|secret|password|passwd|auth|credential|sig|signature)$/i
const FALLBACK_USERINFO_RE = /(:\/\/)([^/@\s]+@)/g
const FALLBACK_QUERY_SECRET_RE = /([?&](?:api[-_]?key|key|token|access[-_]?token|secret|password|passwd|auth|credential|sig|signature)=)[^&#]*/gi

export interface ConsoleContextSettings {
  timeoutMs?: number
  sseDurationMs?: number
  sseMaxEvents?: number
  followRedirects?: boolean
  cooldownMs?: number
}

export interface WebSocketContextSettings {
  sendIntervalMs?: number
  maxEvents?: number
  connectTimeoutMs?: number
  maxDurationMs?: number
  pingIntervalMs?: number
  cooldownMs?: number
}

export interface TaskContextSettings {
  testsPerUrl?: number
  allowWrites?: boolean
  observationMs?: number
  wallTimeSeconds?: number
  cooldownMs?: number
}

export interface TaskDraftEndpointSummary {
  method: string
  path: string
  protocol: string
  name?: string
}

export interface ConsoleTestContext extends AssistantCurrentTestContext {
  page: 'console'
  settings?: Record<string, unknown>
}

export interface WebSocketTestContext extends AssistantCurrentTestContext {
  page: 'websocket'
  settings?: Record<string, unknown>
}

export interface TaskTestContext extends AssistantCurrentTestContext {
  page: 'tasks'
  settings?: Record<string, unknown>
  draft_endpoints?: Array<Record<string, unknown>>
}

export interface ModelTestingContextModelPreview {
  id: string
  owned_by: string | null
  model_type: string
  availability: string
  latency_ms: number | null
  availability_detail: string | null
  tool_calling: string
  streaming: string
  ttft_ms: number | null
  structured_output: string
  input_modalities: string[]
  output_modalities: string[]
}

export interface ModelTestingContext {
  page: 'model_testing'
  has_result: boolean
  config: {
    base_url: string
    protocol: string
    use_saved_key: boolean
    has_custom_api_key: boolean
    timeout_seconds: number
    checks: string[]
    cooldown_ms: number
  }
  summary: {
    total: number
    available: number
    unavailable: number
    tool_supported: number
    streaming: number
    structured: number
  }
  active_model: ModelTestingContextModelPreview | null
  models_preview: ModelTestingContextModelPreview[]
}

export type AnyAssistantTestContext =
  | ConsoleTestContext
  | WebSocketTestContext
  | TaskTestContext
  | ModelTestingContext

/** 对请求头/响应头中的敏感字段进行浏览器端前置脱敏。 */
export function redactSnapshotHeaders(
  headers: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  if (!headers) return {}
  const safe: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(headers)) {
    if (SENSITIVE_HEADER_RE.test(key)) {
      safe[key] = Array.isArray(value) ? value.map(() => '[redacted]') : '[redacted]'
    } else {
      safe[key] = value
    }
  }
  return safe
}

/** 剥离或脱敏 URL 中的 userinfo 凭据与敏感查询参数，保持干净 URL 原样不变。 */
export function redactSnapshotUrl(url: string | null | undefined): string {
  if (!url) return ''
  const trimmed = url.trim()
  if (!trimmed) return ''
  try {
    const parsed = new URL(trimmed)
    const hasUserinfo = Boolean(parsed.username || parsed.password)
    let hasSensitiveQuery = false
    for (const [key] of parsed.searchParams.entries()) {
      if (SENSITIVE_QUERY_KEY_RE.test(key)) {
        hasSensitiveQuery = true
        break
      }
    }
    if (!hasUserinfo && !hasSensitiveQuery) return trimmed
    const authority = `${hasUserinfo ? '[redacted]@' : ''}${parsed.host}`
    const queryParts: string[] = []
    for (const [key, value] of parsed.searchParams.entries()) {
      const encodedKey = encodeURIComponent(key)
      queryParts.push(
        SENSITIVE_QUERY_KEY_RE.test(key)
          ? `${encodedKey}=[redacted]`
          : `${encodedKey}=${encodeURIComponent(value)}`,
      )
    }
    const query = queryParts.length ? `?${queryParts.join('&')}` : ''
    const pathname = parsed.pathname === '/' && !trimmed.includes(`${parsed.host}/`) ? '' : parsed.pathname
    return `${parsed.protocol}//${authority}${pathname}${query}${parsed.hash}`
  } catch {
    return trimmed
      .replace(FALLBACK_USERINFO_RE, '$1[redacted]@')
      .replace(FALLBACK_QUERY_SECRET_RE, '$1[redacted]')
  }
}

function clipText(value: string | null | undefined, maxChars = 6000): string | null {
  if (value == null) return null
  if (value.length <= maxChars) return value
  return `${value.slice(0, maxChars)}\n...[truncated ${value.length - maxChars} chars]`
}

function serializeConsoleSettings(settings?: ConsoleContextSettings): Record<string, unknown> | undefined {
  if (!settings) return undefined
  const out: Record<string, unknown> = {}
  if (settings.timeoutMs !== undefined) out.timeout_ms = settings.timeoutMs
  if (settings.sseDurationMs !== undefined) out.sse_duration_ms = settings.sseDurationMs
  if (settings.sseMaxEvents !== undefined) out.sse_max_events = settings.sseMaxEvents
  if (settings.followRedirects !== undefined) out.follow_redirects = settings.followRedirects
  if (settings.cooldownMs !== undefined) out.cooldown_ms = settings.cooldownMs
  return Object.keys(out).length ? out : undefined
}

function serializeWebSocketSettings(settings?: WebSocketContextSettings): Record<string, unknown> | undefined {
  if (!settings) return undefined
  const out: Record<string, unknown> = {}
  if (settings.sendIntervalMs !== undefined) out.send_interval_ms = settings.sendIntervalMs
  if (settings.maxEvents !== undefined) out.max_events = settings.maxEvents
  if (settings.connectTimeoutMs !== undefined) out.connect_timeout_ms = settings.connectTimeoutMs
  if (settings.maxDurationMs !== undefined) out.max_duration_ms = settings.maxDurationMs
  if (settings.pingIntervalMs !== undefined) out.ping_interval_ms = settings.pingIntervalMs
  if (settings.cooldownMs !== undefined) out.cooldown_ms = settings.cooldownMs
  return Object.keys(out).length ? out : undefined
}

function serializeTaskSettings(settings?: TaskContextSettings): Record<string, unknown> | undefined {
  if (!settings) return undefined
  const out: Record<string, unknown> = {}
  if (settings.testsPerUrl !== undefined) out.tests_per_url = settings.testsPerUrl
  if (settings.allowWrites !== undefined) out.allow_writes = settings.allowWrites
  if (settings.observationMs !== undefined) out.observation_ms = settings.observationMs
  if (settings.wallTimeSeconds !== undefined) out.wall_time_seconds = settings.wallTimeSeconds
  if (settings.cooldownMs !== undefined) out.cooldown_ms = settings.cooldownMs
  return Object.keys(out).length ? out : undefined
}

function normalizeChecks(checks: Record<string, boolean> | readonly string[] | undefined): string[] {
  if (!checks) return []
  if (Array.isArray(checks)) return checks.map((item) => String(item)).filter(Boolean)
  return Object.entries(checks)
    .filter(([, enabled]) => Boolean(enabled))
    .map(([name]) => name)
}

function summarizeModelProfile(model: ModelProfile): ModelTestingContextModelPreview {
  return {
    id: model.id,
    owned_by: model.owned_by ?? null,
    model_type: model.model_type,
    availability: model.availability?.status ?? 'untested',
    latency_ms: model.availability?.latency_ms ?? null,
    availability_detail: clipText(model.availability?.detail ?? null, 300),
    tool_calling: model.tool_calling?.status ?? 'untested',
    streaming: model.streaming?.status ?? 'untested',
    ttft_ms: model.streaming?.ttft_ms ?? null,
    structured_output: model.structured_output?.status ?? 'untested',
    input_modalities: (model.input_modalities ?? [])
      .filter((item) => item.status === 'supported' || item.status === 'inferred')
      .map((item) => item.modality),
    output_modalities: (model.output_modalities ?? [])
      .filter((item) => item.status === 'supported' || item.status === 'inferred')
      .map((item) => item.modality),
  }
}

/** 构造接口测试页（HTTP/SSE）的当前测试快照。 */
export function buildConsoleTestContext(params: {
  method: string
  url: string
  headers: Record<string, string>
  bodyMode: string
  body: string
  result: ConsoleResult | null
  liveEvents: ConsoleSseEvent[] | null
  localFailure: ResultPresentation | null
  responseCleared: boolean
  settings?: ConsoleContextSettings
}): ConsoleTestContext {
  const request = {
    method: params.method,
    url: redactSnapshotUrl(params.url),
    headers: redactSnapshotHeaders(params.headers),
    body_mode: params.bodyMode,
    body: params.bodyMode === 'none' ? null : clipText(params.body, 3000),
  }
  const settings = serializeConsoleSettings(params.settings)
  const extra = settings ? { settings } : {}

  if (params.responseCleared) {
    return { page: 'console', has_result: false, request, response: null, error: null, ...extra }
  }

  if (params.localFailure) {
    return {
      page: 'console',
      has_result: true,
      request,
      response: null,
      error: {
        code: params.localFailure.code,
        title: params.localFailure.title,
        message: params.localFailure.message,
        result_status: params.localFailure.resultStatus ?? null,
      },
      ...extra,
    }
  }

  if (params.result) {
    const resp = params.result.response
    return {
      page: 'console',
      has_result: true,
      request,
      response: resp
        ? {
            status: resp.status,
            reason: resp.reason,
            elapsed_ms: resp.elapsed_ms,
            body_bytes: resp.body_bytes,
            body_truncated: resp.body_truncated,
            headers: redactSnapshotHeaders(resp.headers),
            body: clipText(resp.body, 6000),
            redirects: resp.redirects,
            sse: resp.sse
              ? {
                  stop_reason: resp.sse.stop_reason,
                  event_count: resp.sse.events.length,
                  events: resp.sse.events.slice(-20).map((evt) => ({
                    event: evt.event,
                    data: clipText(evt.data, 400),
                  })),
                }
              : null,
          }
        : null,
      error: params.result.ok
        ? null
        : {
            result_code: params.result.result_code ?? null,
            error_kind: params.result.error_kind,
            error_message: params.result.error_message,
            elapsed_ms: params.result.elapsed_ms,
          },
      ...extra,
    }
  }

  if (params.liveEvents && params.liveEvents.length > 0) {
    return {
      page: 'console',
      has_result: true,
      request,
      response: {
        sse: {
          stop_reason: 'streaming',
          event_count: params.liveEvents.length,
          events: params.liveEvents.slice(-20).map((evt) => ({
            event: evt.event,
            data: clipText(evt.data, 400),
          })),
        },
      },
      error: null,
      ...extra,
    }
  }

  return { page: 'console', has_result: false, request, response: null, error: null, ...extra }
}

/** 构造长连接测试页（WebSocket）的当前会话与时间线快照。 */
export function buildWebSocketTestContext(params: {
  url: string
  headers: Record<string, string>
  subprotocols: string[]
  session: StreamSession | null
  events: StreamEvent[]
  settings?: WebSocketContextSettings
}): WebSocketTestContext {
  const request = {
    url: redactSnapshotUrl(params.url),
    headers: redactSnapshotHeaders(params.headers),
    subprotocols: params.subprotocols,
  }
  const settings = serializeWebSocketSettings(params.settings)
  const extra = settings ? { settings } : {}

  if (!params.session) {
    return { page: 'websocket', has_result: false, request, session: null, events: [], ...extra }
  }
  return {
    page: 'websocket',
    has_result: true,
    request,
    session: {
      id: params.session.id,
      status: params.session.status,
      finished: params.session.finished,
      url: redactSnapshotUrl(params.session.url),
      http_status: params.session.http_status,
      subprotocol: params.session.subprotocol,
      stop_reason: params.session.stop_reason,
      error_kind: params.session.error_kind,
      error_message: params.session.error_message,
      close_code: params.session.close_code,
      close_reason: params.session.close_reason,
      event_count: params.session.event_count,
      sent_count: params.session.sent_count,
      byte_count: params.session.byte_count,
    },
    events: params.events.slice(-25).map((event) => ({
      sequence: event.sequence,
      direction: event.direction,
      kind: event.kind,
      elapsed_ms: event.elapsed_ms,
      data: clipText(event.data, 350),
    })),
    ...extra,
  }
}

/** 构造智能群测页的当前运行快照。 */
export function buildTaskTestContext(params: {
  origin: string
  run: TaskRecord | null
  resultsCleared: boolean
  draftEndpointCount: number
  draftCaseCount: number
  settings?: TaskContextSettings
  draftEndpoints?: TaskDraftEndpointSummary[]
}): TaskTestContext {
  const settings = serializeTaskSettings(params.settings)
  const draftEndpoints = params.draftEndpoints?.slice(0, 50).map((endpoint) => ({
    method: endpoint.method,
    path: endpoint.path,
    protocol: endpoint.protocol,
    ...(endpoint.name ? { name: endpoint.name } : {}),
  }))
  const extra: Pick<TaskTestContext, 'settings' | 'draft_endpoints'> = {
    ...(settings ? { settings } : {}),
    ...(draftEndpoints ? { draft_endpoints: draftEndpoints } : {}),
  }

  if (!params.run || params.resultsCleared) {
    return {
      page: 'tasks',
      has_result: false,
      request: {
        origin: redactSnapshotUrl(params.origin),
        draft_endpoints: params.draftEndpointCount,
        draft_cases: params.draftCaseCount,
      },
      task: null,
      ...extra,
    }
  }
  return {
    page: 'tasks',
    has_result: true,
    request: { origin: redactSnapshotUrl(params.run.snapshot.origin || params.origin) },
    task: {
      id: params.run.id,
      title: params.run.title ?? null,
      status: params.run.status,
      stage: params.run.stage,
      error_kind: params.run.error_kind,
      total_cases: params.run.snapshot.cases.length,
      recorded_cases: params.run.results.length,
      summary: clipText(params.run.summary, 2500),
      results: params.run.results.slice(0, 30).map((item) => {
        const evidence = item.evidence as Record<string, unknown> | undefined
        const evResp = evidence?.response as Record<string, unknown> | undefined
        return {
          index: item.index,
          name: item.name,
          protocol: item.protocol,
          status: item.status,
          reason: item.reason ?? null,
          failed_assertions: item.assertions.filter((a) => a.passed !== true).slice(0, 6),
          extracted: item.extracted?.slice(0, 6) ?? [],
          http_status: evResp?.status ?? null,
          error_kind: evidence?.error_kind ?? null,
          response_preview:
            typeof evResp?.body === 'string' ? clipText(evResp.body, 500) : null,
        }
      }),
    },
    ...extra,
  }
}

export interface ModelTestingContextInput {
  baseUrl: string
  protocol: ModelProtocol | string
  useSavedKey: boolean
  hasCustomApiKey?: boolean
  /** 仅用于推导 has_custom_api_key 布尔标记，绝不输出任何明文凭据。 */
  apiKey?: string | null
  timeoutSeconds: number
  checks: Record<string, boolean> | readonly string[]
  cooldownMs?: number
  models?: ModelProfile[]
  activeModelId?: string | null
  activeModel?: ModelProfile | null
  hasResult?: boolean
}

/** 构造大模型测试页的脱敏上下文快照，确保绝不包含任何原始 API Key。 */
export function buildModelTestingContext(input: ModelTestingContextInput): ModelTestingContext {
  const models = input.models ?? []
  const hasCustomApiKey =
    typeof input.hasCustomApiKey === 'boolean'
      ? input.hasCustomApiKey
      : Boolean(input.apiKey && input.apiKey.trim())
  const activeProfile =
    input.activeModel ??
    (input.activeModelId ? models.find((item) => item.id === input.activeModelId) ?? null : null)

  let available = 0
  let unavailable = 0
  let toolSupported = 0
  let streaming = 0
  let structured = 0
  for (const item of models) {
    const avail = item.availability?.status
    if (avail === 'available') available++
    else if (avail === 'unavailable' || avail === 'error') unavailable++
    if (item.tool_calling?.status === 'supported') toolSupported++
    if (item.streaming?.status === 'supported') streaming++
    if (item.structured_output?.status === 'supported') structured++
  }

  return {
    page: 'model_testing',
    has_result: typeof input.hasResult === 'boolean' ? input.hasResult : models.length > 0,
    config: {
      base_url: redactSnapshotUrl(input.baseUrl),
      protocol: input.protocol,
      use_saved_key: Boolean(input.useSavedKey),
      has_custom_api_key: hasCustomApiKey,
      timeout_seconds: input.timeoutSeconds,
      checks: normalizeChecks(input.checks),
      cooldown_ms: typeof input.cooldownMs === 'number' ? input.cooldownMs : 500,
    },
    summary: {
      total: models.length,
      available,
      unavailable,
      tool_supported: toolSupported,
      streaming,
      structured,
    },
    active_model: activeProfile ? summarizeModelProfile(activeProfile) : null,
    models_preview: models.slice(0, 30).map(summarizeModelProfile),
  }
}
