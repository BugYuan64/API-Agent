/** 管理 API 与页面之间的数据契约：集中声明输入、分页、执行证据及编排结果。仅提供静态类型，不执行数据校验；可空字段及可选字段均需由消费页面处理。 */
/** 与后端管理 API 一一对应的类型。
 *  可空字段允许值为 null，可选字段允许省略；两者含义不同。
 *  类型声明不执行运行时校验，页面需显式处理历史记录与缺失值。 */

import type { PageScope } from '@/stores/workbenchRuns'

export type { PageScope }

/** 控制台允许选择的 HTTP 方法；后端仍需校验方法与目标授权范围。 */
export type HttpMethod = 'GET' | 'HEAD' | 'OPTIONS' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
/** 请求正文编码选项；none 表示不发送正文，其他值由后端按对应格式处理。 */
export type BodyMode = 'none' | 'json' | 'text' | 'form'
/** streams 管理接口现在仅负责 WebSocket 会话。 */
export type StreamKind = 'websocket'

/** 本项目对执行结果的分类；独立于目标 HTTP 状态。可选以兼容旧历史与旧服务。 */
export interface ResultMetadata {
  result_code?: string
  result_status?: number | null
}

/** 普通管理错误结构（含框架级 404）；控制台传输失败改用 ConsoleResult 分类信封。 */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details: { field: string; reason: string }[]
    request_id: string
  }
}

/** 分页窗口与服务端总数；total 不等于本次 items 数量。 */
export interface PageInfo {
  limit: number
  offset: number
  total: number
}

/** 列表接口的通用响应包，记录和分页元信息保持在同一响应中。 */
export interface Paged<T> {
  items: T[]
  page: PageInfo
}

/** 管理服务健康与运行模式摘要，不代表目标服务或所有执行引擎均可用。 */
export interface Health {
  status: string
  service: string
  version: string
  llm_mode: string
}

/* ---------- 项目与环境 ---------- */

/** 项目归属记录，作为环境和执行关联入口；不直接保存环境凭据。 */
export interface Project {
  id: string
  name: string
  description: string
  created_at: string
}

/** 认证材料引用：仅记录服务端环境变量名称及注入位置，不包含密钥明文。 */
export interface AuthReference {
  kind: 'bearer' | 'api_key' | 'header'
  env_var: string
  header_name: string | null
}

/** 环境重放授权策略；非幂等重放必须单独允许，不能由前端默认推断。 */
export interface ReplayPolicy {
  allow_replay: boolean
  allow_non_idempotent: boolean
}

/** 不可变环境配置快照，保存授权 origin、方法、路径、认证引用和敏感字段规则。 */
export interface EnvironmentVersion {
  id: string
  environment_id: string
  version: number
  origin: string
  allowed_methods: string[]
  path_templates: string[]
  auth_ref: AuthReference | null
  replay_policy: ReplayPolicy
  sensitive_field_rules: string[]
  created_at: string
}

/** 环境身份与当前配置；current_version 用于追加版本时的乐观锁。 */
export interface Environment {
  id: string
  project_id: string
  name: string
  current_version: number
  created_at: string
  config: EnvironmentVersion
}

/** 环境的全部已保存版本；查看历史不改变当前版本。 */
export interface EnvironmentHistory {
  id: string
  project_id: string
  name: string
  current_version: number
  created_at: string
  versions: EnvironmentVersion[]
}

/** 创建环境的输入：配置字段 + 名称，与后端 EnvironmentInput 对应。 */
export interface EnvironmentInput {
  name: string
  origin: string
  allowed_methods: string[]
  path_templates: string[]
  auth_ref: AuthReference | null
  replay_policy: ReplayPolicy
  sensitive_field_rules: string[]
}

/* ---------- 控制台请求 ---------- */

/** 后端 HTTP 调试请求输入；timeout_ms 为目标请求限制，独立于管理接口 fetch 截止时间。 */
export interface ConsoleRequestInput {
  method: HttpMethod
  url: string
  headers: Record<string, string>
  body_mode: BodyMode
  body?: string | null
  timeout_ms: number
  follow_redirects: boolean
  sse_max_events?: number
  sse_duration_ms?: number
}

/** 重定向链路的一跳：取自目标真实回包，不是本项目推断。 */
export interface RedirectHop {
  status: number
  reason: string | null
  /** 这一跳 Location 指向的下一跳请求地址。 */
  url: string
  /** 跨源跳转：该跳发生时鉴权与 Cookie 已被剥离。 */
  cross_origin: boolean
}

/** 控制台历史详情与子用例中使用的重定向跳点别名。 */
export type ConsoleRedirect = RedirectHop

export interface ConsoleSseEvent {
  event: string
  data: string
}

/** 控制台 SSE 响应快照结构。 */
export interface ConsoleSseResult {
  events: ConsoleSseEvent[]
  stop_reason: string
}

export type ConsoleSseProgress = { type: 'sse_start' } | { type: 'sse_event'; event: ConsoleSseEvent }

/** 目标 HTTP 响应证据；正文可能截断或为空，headers 保留同名头的多个值。 */
export interface ConsoleResponse {
  status: number
  reason: string | null
  http_version: string | null
  headers: Record<string, string[]>
  body: string | null
  body_bytes: number
  body_truncated: boolean
  body_encoding: string | null
  elapsed_ms: number
  redirects: RedirectHop[]
  sse?: ConsoleSseResult | null
}

/** 目标请求结果：ok 描述传输结果，HTTP 状态仍需单独查看；失败时 response 可空。 */
export interface ConsoleResult extends ResultMetadata {
  ok: boolean
  history_id?: string | null
  request: { method: string; url: string }
  response: ConsoleResponse | null
  error_kind: string | null
  error_message: string | null
  elapsed_ms: number
}

/** AI 多轮接口测试中单条子用例的脱敏只读回放快照。 */
export interface ConsoleSubCase extends ResultMetadata {
  id: string
  parent_id: string
  round_index: number
  case_index: number
  label: string
  method: string
  url: string
  request_headers: Record<string, unknown>
  body_mode: string
  request_body: string | null
  ok: boolean
  status_code: number | null
  elapsed_ms: number | null
  error_kind: string | null
  error_message: string | null
  response_headers: Record<string, unknown>
  response_preview: string | null
  response_bytes: number
  http_version: string | null
  response_reason: string | null
  redirects: ConsoleRedirect[]
  sse: ConsoleSseResult | null
  assertion_summary: {
    passed: boolean
    checks: Array<{
      kind: string
      expected: unknown
      actual: unknown
      passed: boolean
    }>
  } | null
  created_at: string
}

/** 脱敏 HTTP 历史预览；正文和头不能当作完整原始请求或响应用于无损还原。 */
export interface ConsoleHistory extends ResultMetadata {
  id: string
  project_id: string | null
  method: string
  url: string
  request_headers: Record<string, unknown>
  body_mode: string
  body_preview: string | null
  request_body?: string | null
  ok: boolean
  status_code: number | null
  elapsed_ms: number
  error_kind: string | null
  error_message: string | null
  response_headers: Record<string, unknown>
  response_preview: string | null
  response_bytes: number
  source?: 'manual' | 'ai'
  title?: string | null
  badge?: string | null
  status?: string
  summary?: string | null
  total_rounds?: number
  cooldown_ms?: number
  sub_case_count?: number
  http_version?: string | null
  response_reason?: string | null
  redirects?: ConsoleRedirect[] | null
  sse?: ConsoleSseResult | null
  sub_cases?: ConsoleSubCase[]
  created_at: string
}

/** 控制台历史列表项别名（与 ConsoleHistory 保持完全兼容）。 */
export type ConsoleHistoryItem = ConsoleHistory

/** 控制台单条历史详情，包含完整请求体、重定向、SSE 快照及子用例列表。 */
export interface ConsoleHistoryDetail extends ConsoleHistory {
  request_body?: string | null
  redirects?: ConsoleRedirect[] | null
  sse?: ConsoleSseResult | null
  sub_cases?: ConsoleSubCase[]
}

/** 后台控制台单次请求或 AI 多轮套件的运行摘要（启动响应与跨页重连共用）。 */
export interface ConsoleRunSummary extends ResultMetadata {
  id: string
  source: 'manual' | 'ai' | string
  title: string | null
  badge: string | null
  status: string
  stage: string
  method: string
  url: string
  ok: boolean
  status_code: number | null
  elapsed_ms: number
  error_kind: string | null
  error_message: string | null
  total_rounds: number
  current_round: number
  total_cases: number
  completed_cases: number
  cooldown_ms: number
  cooldown_remaining_ms: number | null
  summary: string | null
  created_at: string | null
}

/** 当前仍在后台执行的控制台运行列表。 */
export interface ConsoleActiveRunsResponse {
  items: ConsoleRunSummary[]
}

/** 控制台后台运行可重连 NDJSON 实时帧。 */
export type ConsoleRunLiveFrame =
  | ConsoleSseProgress
  | ({ type: 'progress' } & ConsoleRunSummary)
  | {
      type: 'case_done'
      run_id: string
      round: number
      case_index: number
      completed_cases: number
      total_cases: number
      case: ConsoleSubCase
      case_link: CaseLinkBlock
    }
  | {
      type: 'cooling_down'
      run_id: string
      round: number
      cooldown_ms: number
      cooldown_remaining_ms: number
    }
  | { type: 'result'; result: ConsoleResult }
  | {
      type: 'done'
      run_id: string
      status: string
      ok: boolean
      error_kind?: string | null
      summary?: string | null
      case_links?: CaseLinkBlock[]
      record?: ConsoleHistoryDetail
    }
  | {
      type: 'error'
      status?: number
      code?: string
      message?: string
      error?: { code: string; message: string; request_id?: string }
    }
  | { type: 'heartbeat' }

/* ---------- 长连接 ---------- */

/** 后端长连接启动配置与限额；send 为预排消息，project_id 可不关联项目。 */
export interface StreamStartInput {
  kind: StreamKind
  url: string
  headers: Record<string, string>
  subprotocols: string[]
  max_events: number
  max_duration_ms: number
  connect_timeout_ms: number
  ping_interval_ms: number
  send: string[]
  send_interval_ms: number
  project_id: string | null
}

/** 会话连接状态；事件是否读尽另看分页 has_more，不能仅凭状态判断回放结束。 */
export type StreamStatus =
  | 'queued'
  | 'connecting'
  | 'running'
  | 'open'
  | 'closed'
  | 'completed'
  | 'error'
  | 'cancelled'
  | 'interrupted'

/** 长连接会话摘要与计数；finished、stop_reason 和错误字段共同描述终止边界。 */
export interface StreamSession extends ResultMetadata {
  id: string
  kind: StreamKind
  url: string
  status: StreamStatus
  finished: boolean
  stop_reason: string | null
  error_kind: string | null
  error_message: string | null
  http_status: number | null
  content_type: string | null
  subprotocol: string | null
  close_code: number | null
  close_reason: string | null
  event_count: number
  byte_count: number
  sent_count: number
  truncated_count: number
  opened_at: string | null
  closed_at: string | null
  source?: 'manual' | 'ai' | string
  title?: string | null
  badge?: string | null
  parent_session_id?: string | null
  round_index?: number
  case_index?: number
  case_label?: string | null
  cooldown_ms?: number
  total_rounds?: number
  summary?: string | null
  sub_case_count?: number
}

/** 带方向、序号与相对耗时的事件证据；data 可能截断，二进制事件使用编码文本。 */
export interface StreamEvent {
  session_id: string
  sequence: number
  direction: 'in' | 'out'
  kind: string
  event_name: string | null
  data: string | null
  data_bytes: number
  truncated: boolean
  elapsed_ms: number
}

/** 游标事件页：finished 表示会话结束，has_more 表示仍有待读取事件，两者相互独立。 */
export interface StreamEventPage {
  session: StreamSession
  events: StreamEvent[]
  next_cursor: number
  finished: boolean
  has_more: boolean
}

/** 已保存会话摘要及请求配置预览；完整事件通过会话游标接口另外读取。 */
export interface StreamReplayInputs {
  url: string
  request_headers: Record<string, string>
  subprotocols: string[]
  scheduled_messages: string[] | null
}

export interface StreamHistory extends ResultMetadata {
  id: string
  kind: string
  url: string
  request_headers: Record<string, unknown>
  status: string
  stop_reason: string | null
  error_kind: string | null
  error_message: string | null
  http_status: number | null
  content_type: string | null
  subprotocol: string | null
  close_code: number | null
  close_reason: string | null
  event_count: number
  byte_count: number
  sent_count: number
  truncated_count: number
  settings: Record<string, unknown>
  opened_at: string | null
  closed_at: string | null
  created_at: string
  source?: 'manual' | 'ai' | string
  title?: string | null
  badge?: string | null
  parent_session_id?: string | null
  round_index?: number
  case_index?: number
  case_label?: string | null
  cooldown_ms?: number
  total_rounds?: number
  summary?: string | null
  sub_case_count?: number
}

/** 长连接历史列表项别名（与 StreamHistory 保持完全兼容）。 */
export type StreamHistoryItem = StreamHistory

/** 跨页切回重连时暴露的单个活跃长连接或 AI 套件运行摘要。 */
export interface ActiveStreamItem {
  id: string
  kind: 'manual' | 'ai_suite'
  source: 'manual' | 'ai' | string
  url: string
  title: string | null
  badge: string | null
  status: string
  stage: string | null
  current_round: number
  total_rounds: number
  completed_cases: number
  total_cases: number
  cooldown_ms: number
  cooldown_remaining_ms: number | null
  opened_at: string | null
}

/** 当前正在后台运行的手动长连接与 AI 多轮测试套件列表。 */
export interface StreamActiveResponse {
  items: ActiveStreamItem[]
  manual_sessions: ActiveStreamItem[]
  ai_suites: ActiveStreamItem[]
}

/** 归属于一条 AI WebSocket 历史记录的单个子用例摘要。 */
export interface StreamSubCaseSummary extends ResultMetadata {
  id: string
  parent_session_id: string
  round_index: number
  case_index: number
  case_label: string | null
  label: string
  url: string
  status: string
  stop_reason: string | null
  error_kind: string | null
  error_message: string | null
  http_status: number | null
  subprotocol: string | null
  close_code: number | null
  close_reason: string | null
  event_count: number
  byte_count: number
  sent_count: number
  truncated_count: number
  opened_at: string | null
  closed_at: string | null
  created_at: string
}

/** 一条分组 AI WebSocket 历史下的全部子用例列表。 */
export interface StreamSubCaseListResponse {
  session_id: string
  items: StreamSubCaseSummary[]
  total: number
}

/** 单条 WebSocket 子用例的完整只读回放快照（连接参数 + 会话状态 + 事件流）。 */
export interface StreamSubCaseDetail {
  session_id: string
  case_id: string
  round_index: number
  case_index: number
  case_label: string | null
  label: string
  readonly: boolean
  inputs: StreamReplayInputs
  session: StreamSession
  events: StreamEvent[]
}

/** AI WebSocket 多轮测试套件的状态与进度快照。 */
export interface StreamSuiteStatus {
  id: string
  status: string
  stage: string
  source: 'manual' | 'ai' | string
  title: string | null
  badge: string | null
  url: string
  current_round: number
  total_rounds: number
  completed_cases: number
  total_cases: number
  passed_cases?: number
  failed_cases?: number
  error_cases?: number
  cooldown_ms: number
  cooldown_remaining_ms: number | null
  current_case_index?: number | null
  current_case_label?: string | null
  summary: string | null
  error_kind: string | null
  error_message: string | null
  case_links?: CaseLinkBlock[]
}

/** AI WebSocket 多轮测试套件的可重连 NDJSON 实时帧。 */
export type StreamSuiteLiveFrame =
  | ({ type: 'progress'; suite_id: string } & Omit<StreamSuiteStatus, 'case_links'>)
  | { type: 'case_done'; suite_id: string; case_link: CaseLinkBlock }
  | {
      type: 'cooling_down'
      suite_id: string
      current_round: number
      total_rounds: number
      cooldown_ms: number
      cooldown_remaining_ms: number | null
    }
  | ({ type: 'done'; suite_id: string } & StreamSuiteStatus)
  | { type: 'heartbeat' }
  | { type: 'error'; code?: string; message?: string }

/* ---------- 计划执行 ---------- */

/** 单条断言的确定性结果；passed 为 null 表示没有可确认的通过或失败结论。 */
export interface AssertionResult {
  index: number
  kind: string
  passed: boolean | null
  message: string | null
}

/** 单条提取变量的脱敏观测结果。 */
export interface ExtractedVariable {
  name: string
  source: 'json' | 'header' | 'status'
  ok: boolean
  sensitive: boolean
  value: unknown
  message: string | null
}

/** 单个请求用例的执行观测及断言；未知请求和异常必须保留，不能自动当作断言失败。 */
export interface CaseResult extends ResultMetadata {
  id: string
  status: 'passed' | 'failed' | 'timed_out' | 'cancelled' | 'error' | 'skipped' | 'unknown'
  http_status: number | null
  response_time_ms: number | null
  error_kind: string | null
  assertions: AssertionResult[]
  extracted?: ExtractedVariable[]
}

/** 已落库的计划执行结果及预算观测；旧记录的计划可能缺失，状态由后端裁决。 */
export interface Execution extends ResultMetadata {
  id: string
  /** 执行时保存的计划；旧记录可能缺失，不能从当前编辑器补造。 */
  plan: Record<string, unknown> | null
  plan_sha256: string | null
  project_id: string | null
  label: string
  engine: string
  status: 'passed' | 'failed' | 'error' | 'timed_out' | 'cancelled'
  error_kind: string | null
  duration_ms: number
  reserved_requests: number
  observed_attempts: number
  unknown_requests: number
  executions: CaseResult[]
  created_at: string
}

/** 执行引擎探测状态；不可用时版本等成功元信息可能不存在。 */
export interface EngineStatus {
  available: boolean
  version?: string
  format?: string
  engine?: string
  error_kind?: string
}

/** 单个执行驱动（原生 Python / Postman CLI）的探测结果。 */
export interface EngineInfo extends EngineStatus {
  id: string
}

/* ---------- 大模型动态配置 ---------- */

/** 可回显的模型配置；密钥仅返回 has_api_key 标志，禁止据此推导或还原密钥。 */
export interface LLMConfig {
  enabled: boolean
  provider: string
  base_url: string | null
  model: string
  generation_model: string | null
  timeout_seconds: number
  has_api_key: boolean
}

/** 模型配置更新输入；api_key 省略保留旧值、空串清空，地址允许为 null。 */
export interface LLMConfigUpdate {
  enabled: boolean
  provider?: string
  base_url: string | null
  api_key?: string | null
  model: string
  generation_model?: string | null
  timeout_seconds?: number
}

/** 模型探测结果与耗时；业务探测失败可作为成功管理响应中的 ok=false 返回。 */
export interface LLMPingResult {
  ok: boolean
  latency_ms: number
  message: string | null
}

/** 模型列表获取结果；ok 与 models 是否为空分别表达请求成功和列表内容。 */
export interface LLMModelListResult {
  ok: boolean
  models: string[]
  count: number
  message: string | null
}

/* ---------- AI 助手小Y 向量知识库 ---------- */

export type KnowledgeIndexStatus = 'unconfigured' | 'unbuilt' | 'stale' | 'ready'
export type KnowledgeRetrievalMode = 'vector' | 'lexical_fallback'

export interface EmbeddingConfig {
  enabled: boolean
  use_llm_endpoint: boolean
  base_url: string | null
  effective_base_url: string | null
  model: string
  dimensions: number | null
  top_k: number
  timeout_seconds: number
  has_api_key: boolean
  has_effective_api_key: boolean
}

export interface EmbeddingConfigUpdate {
  enabled: boolean
  use_llm_endpoint: boolean
  base_url?: string | null
  api_key?: string | null
  model: string
  dimensions?: number | null
  top_k: number
  timeout_seconds?: number
}

export interface KnowledgeDocumentSummary {
  doc_id: string
  category: string
  title: string
  sections: string[]
  chunk_count: number
}

export interface KnowledgeStatus {
  status: KnowledgeIndexStatus
  configured: boolean
  ready: boolean
  vector_store?: string
  collection_name?: string
  retrieval_mode: KnowledgeRetrievalMode
  corpus_hash: string
  chunk_count: number
  vector_count: number
  embedding_model: string | null
  embedding_dimensions: number | null
  built_at: string | null
  last_error: string | null
  config: EmbeddingConfig
  documents: KnowledgeDocumentSummary[]
}

export interface KnowledgeBuildResult {
  ok: boolean
  status: KnowledgeIndexStatus
  vector_store?: string
  collection_name?: string
  chunk_count: number
  vector_count: number
  embedding_model: string
  embedding_dimensions: number
  corpus_hash: string
  built_at: string
  duration_ms: number
  message: string
}

export interface KnowledgeSearchHit {
  chunk_id: string
  doc_id: string
  category: string
  title: string
  section: string
  score: number
  snippet: string
  content: string
}

export interface KnowledgeSearchResult {
  query: string
  mode: KnowledgeRetrievalMode
  hits: KnowledgeSearchHit[]
}

/* ---------- 大模型测试 ---------- */

export type ModelProtocol = 'auto' | 'openai' | 'anthropic' | 'gemini'
export type ResolvedModelProtocol = 'openai' | 'anthropic' | 'gemini'

export type ModelCategory =
  | 'chat'
  | 'reasoning'
  | 'embedding'
  | 'rerank'
  | 'image'
  | 'audio'
  | 'video'
  | 'moderation'
  | 'unknown'

export type ProbeStatus =
  | 'untested'
  | 'supported'
  | 'partial'
  | 'unsupported'
  | 'available'
  | 'unavailable'
  | 'inferred'
  | 'unknown'
  | 'error'

export type ProbeCheckName =
  | 'availability'
  | 'tool_calling'
  | 'input_modalities'
  | 'output_modalities'
  | 'streaming'
  | 'structured_output'

export interface CapabilityResult {
  status: ProbeStatus
  source: 'probed' | 'metadata' | 'inferred' | 'untested'
  latency_ms: number | null
  ttft_ms?: number | null
  detail: string | null
}

export interface ModalityItem {
  modality: 'text' | 'image' | 'audio' | 'video' | 'file' | 'reasoning' | 'embedding' | 'json'
  status: ProbeStatus
  source: 'probed' | 'metadata' | 'inferred' | 'untested'
  detail: string | null
}

export interface ModelProfile {
  id: string
  owned_by: string | null
  created: number | null
  model_type: ModelCategory
  type_source: 'probed' | 'metadata' | 'inferred' | 'unknown'
  availability: CapabilityResult
  tool_calling: CapabilityResult
  streaming?: CapabilityResult
  structured_output?: CapabilityResult
  input_modalities: ModalityItem[]
  output_modalities: ModalityItem[]
  raw_metadata: Record<string, unknown>
}

export interface ModelDiscoverResult {
  ok: boolean
  protocol?: ResolvedModelProtocol
  base_url: string | null
  http_status: number | null
  latency_ms: number
  count: number
  message: string
  models: ModelProfile[]
}

export interface ModelProbeResult {
  ok: boolean
  protocol?: ResolvedModelProtocol
  base_url: string
  tested_count: number
  available_count: number
  tool_supported_count: number
  streaming_supported_count?: number
  structured_output_supported_count?: number
  message: string
  results: ModelProfile[]
}

export type ModelProbeStreamFrame =
  | {
      type: 'start'
      protocol: ResolvedModelProtocol
      base_url: string
      models: string[]
      total: number
      concurrency: number
    }
  | {
      type: 'model_start'
      model_id: string
    }
  | {
      type: 'model_result'
      model_id: string
      model: ModelProfile
      completed: number
      total: number
    }
  | {
      type: 'heartbeat'
    }
  | {
      type: 'done'
      result: ModelProbeResult
    }
  | {
      type: 'error'
      code?: string
      message?: string
    }

/** 持久化模型探测任务的来源标识。 */
export type ModelTestRunSource = 'manual' | 'ai'

/** 持久化模型探测任务的生命周期状态。 */
export type ModelTestRunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'cancelled'
  | 'error'
  | 'interrupted'

/** 启动持久化后台模型探测任务请求（临时 api_key 仅驻留内存，绝不落库）。 */
export interface ModelTestRunStartRequest {
  base_url?: string | null
  api_key?: string | null
  use_saved_key?: boolean
  protocol?: ModelProtocol
  models: string[]
  checks?: ProbeCheckName[]
  timeout_seconds?: number
  concurrency?: number
  cooldown_ms?: number
  rounds?: number
  title?: string | null
  source?: ModelTestRunSource
  badge?: string | null
  discover_meta?: Record<string, unknown> | null
  discovered_profiles?: ModelProfile[] | null
}

/** 单轮单个模型的只读回放快照，供历史查看与 AI 子用例链接点击回放。 */
export interface ModelTestSubCaseSnapshot {
  case_id: string
  round_index: number
  case_index: number
  label: string
  model_id: string
  checks: ProbeCheckName[]
  status: string
  latency_ms: number | null
  ttft_ms: number | null
  request_snapshot: Record<string, unknown>
  observation: Record<string, unknown>
  profile: ModelProfile
  created_at: string | null
}

/** 单次模型探测任务（支持多轮）的结构化汇总指标。 */
export interface ModelTestRunSummary {
  tested_count: number
  total_cases: number
  completed_cases: number
  available_count: number
  unavailable_count: number
  error_count: number
  tool_supported_count: number
  streaming_supported_count: number
  structured_output_supported_count: number
  vision_supported_count: number
  avg_latency_ms: number | null
  avg_ttft_ms: number | null
  completed_rounds: number
  total_rounds: number
  message: string
}

/** 持久化模型探测历史列表项（不包含明文密钥）。 */
export interface ModelTestRunListItem {
  id: string
  title: string | null
  source: ModelTestRunSource
  badge: string | null
  status: ModelTestRunStatus
  error_message: string | null
  base_url: string
  protocol: ModelProtocol
  resolved_protocol: ResolvedModelProtocol | null
  used_saved_key: boolean
  has_custom_api_key: boolean
  timeout_seconds: number
  concurrency: number
  checks: ProbeCheckName[]
  target_models: string[]
  cooldown_ms: number
  total_rounds: number
  completed_rounds: number
  sub_case_count: number
  summary: ModelTestRunSummary | null
  summary_text: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

/** 单次模型探测任务完整详情，包含最终画像快照、各轮汇总与子用例快照。 */
export interface ModelTestRunDetail extends ModelTestRunListItem {
  discover_meta: Record<string, unknown> | null
  models_snapshot: ModelProfile[]
  rounds: Array<Record<string, unknown>>
  sub_cases: ModelTestSubCaseSnapshot[]
}

/** 分页的模型探测任务历史列表响应。 */
export interface ModelTestRunListResponse {
  items: ModelTestRunListItem[]
  page: PageInfo
}

/** 跨页切回重连所需的单个活跃模型探测任务摘要。 */
export interface ModelTestActiveRunItem {
  id: string
  title: string | null
  source: ModelTestRunSource
  badge: string | null
  status: ModelTestRunStatus
  stage: string
  base_url: string
  protocol: ModelProtocol
  resolved_protocol: ResolvedModelProtocol | null
  target_models: string[]
  checks: ProbeCheckName[]
  current_round: number
  total_rounds: number
  completed_rounds: number
  cooldown_ms: number
  cooldown_remaining_ms: number | null
  current_models: string[]
  completed_cases: number
  total_cases: number
  started_at: string | null
  created_at: string
}

/** 当前仍在后台运行的模型探测任务列表。 */
export interface ModelTestActiveRunsResponse {
  items: ModelTestActiveRunItem[]
}

/** 后台模型探测任务实时流共享快照字段。 */
export interface ModelTestLiveSnapshot {
  run_id: string
  title: string | null
  source: ModelTestRunSource
  badge: string | null
  status: ModelTestRunStatus
  stage: string
  base_url: string
  protocol: ModelProtocol
  resolved_protocol: ResolvedModelProtocol | null
  current_round: number
  total_rounds: number
  completed_rounds: number
  cooldown_ms: number
  cooldown_remaining_ms: number | null
  current_models: string[]
  completed_cases: number
  total_cases: number
  models_snapshot: ModelProfile[]
  new_sub_cases: ModelTestSubCaseSnapshot[]
  summary: ModelTestRunSummary | null
  summary_text: string | null
  error_message: string | null
  snapshot?: boolean
}

/** 后台模型探测可重连 NDJSON 实时帧。 */
export type ModelTestLiveFrame =
  | ({ type: 'progress' } & ModelTestLiveSnapshot)
  | ({ type: 'cooling_down' } & ModelTestLiveSnapshot)
  | ({ type: 'done'; run?: ModelTestRunDetail } & ModelTestLiveSnapshot)
  | { type: 'heartbeat' }
  | { type: 'error'; code?: string; message?: string }

/* ---------- AI 助手小Y 后台运行与持久化对话线程 ---------- */

export type CaseLinkTone = 'success' | 'danger' | 'warning' | 'info' | 'neutral'

/** AI 回复中嵌入的可点击子用例只读回放块，跨四页统一契约。 */
export interface CaseLinkBlock {
  type: 'case_link'
  scope: PageScope
  history_id: string
  case_id: string
  round: number
  label: string
  status: string
  tone: CaseLinkTone
}

/** 启动后台 AI 助手运行的请求契约。 */
export interface AssistantRunStartRequest {
  scope: PageScope
  history_id?: string | null
  task_id?: string | null
  messages: Array<{ role: 'user' | 'assistant'; content: string }>
  current_test?: Record<string, unknown> | null
  settings?: Record<string, unknown> | null
  model?: string | null
  base_catalog?: unknown | null
  ephemeral_api_key?: string | null
}

/** 启动后台 AI 助手运行的受理响应（202）。 */
export interface AssistantRunStartResponse {
  run_id: string
  scope: PageScope
  history_id: string | null
  status: string
}

/** 当前仍在后台运行的 AI 助手任务摘要。 */
export interface AssistantActiveRun {
  run_id: string
  scope: PageScope
  history_id: string | null
  status: string
  phase?: string | null
  started_at?: string | null
}

/** 当前活跃 AI 助手运行列表响应。 */
export interface AssistantActiveRunsResponse {
  items: AssistantActiveRun[]
}

/** 服务端持久化的单条 AI 助手消息（可携带结构化子用例链接块）。 */
export interface AssistantThreadMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  blocks?: CaseLinkBlock[]
  run_id?: string | null
  created_at?: string | null
}

/** 绑定到 (scope, history_id) 的服务端持久化 AI 助手对话线程。 */
export interface AssistantThread {
  id: string | null
  scope: PageScope
  history_id: string
  title: string | null
  active_run_id: string | null
  messages: AssistantThreadMessage[]
}

/** AI 助手创建或分叉新测试历史时下发的绑定载荷。 */
export interface AssistantRunBoundPayload {
  scope: PageScope
  history_id: string
  previous_history_id: string | null
  badge?: string | null
  title?: string | null
  forked: boolean
  run_id?: string
}

/** 后台 AI 助手可重连 NDJSON 实时帧。 */
export type AssistantLiveFrame =
  | {
      type: 'status'
      phase: 'thinking' | 'reading_data' | 'testing' | 'cooling_down'
      message?: string
      tool?: string
    }
  | ({ type: 'run_bound' } & AssistantRunBoundPayload)
  | { type: 'case_link'; block: CaseLinkBlock }
  | { type: 'text'; text: string }
  | { type: 'done'; text: string; history_id: string | null; blocks?: CaseLinkBlock[] }
  | { type: 'error'; code: string; message?: string }
  | { type: 'heartbeat' }



/* ---------- AI 编排（/api/agent） ---------- */

/** 一轮中单个请求/断言构成的确定性证据。ID 形如 `r0:case-id:a1`。 */
export interface AgentEvidence {
  id: string
  case_id: string
  status: string
  http_status: number | null
  error_kind: string | null
  assertion_index: number | null
  kind: string | null
  expected: unknown
  passed: boolean | null
  message: string | null
}

/** 模型输出的未证实假设，evidence_ids 必须引用已有证据。 */
export interface AgentHypothesis {
  id: string
  cause: string
  evidence_ids: string[]
  next_check: string
}

/** 模型生成的总结、假设和覆盖缺口；不替代确定性执行证据。 */
export interface AgentAnalysis {
  summary: string
  hypotheses: AgentHypothesis[]
  coverage_gaps: string[]
}

/** 诊断证据与模型分析的组合，两类内容保持独立便于核对引用。 */
export interface AgentDiagnosis {
  evidence: AgentEvidence[]
  analysis: AgentAnalysis
}

/** 模型建议的受限补测变更，关联父用例和假设；是否允许执行由后端范围规则裁决。 */
export interface AgentMutation {
  parent_id: string
  hypothesis_id: string
  location: 'body' | 'query' | 'path'
  pointer: string
  action: 'set' | 'remove'
  value: unknown
  assertions: unknown[]
  reason: string
}

/** 轮次后的继续测试建议、候选计划与拒绝原因；建议不代表已实际执行。 */
export interface AgentAdaptation {
  continue_testing: boolean
  reason: string
  plan: AgentPlan | null
  mutations: AgentMutation[]
  rejected: string[]
}

/** Agent 产物里的执行计划（与 ExecutionPlan 同构，此处只声明页面用到的字段）。 */
export interface AgentPlan {
  origin: string
  allowed_methods: string[]
  path_templates: string[]
  requests: AgentRequestCase[]
  max_requests: number
}

/** 编排计划中的请求和断言定义；query/body 可缺省，执行仍须满足授权范围。 */
export interface AgentRequestCase {
  id: string
  method: string
  path: string
  query?: Record<string, string>
  body?: unknown
  assertions: { kind: string; pointer?: string; expected?: unknown }[]
}

/** 用例设计意图及来源：contract 表示契约依据，inferred 表示推断，不保证失败即为目标缺陷。 */
export interface AgentCaseIntent {
  case_id: string
  category: 'positive' | 'boundary' | 'negative' | 'auth'
  basis: 'contract' | 'inferred'
  rationale: string
  source_quote: string | null
}

/** 单轮编排快照，关联计划、意图、执行证据、诊断与补测建议。 */
export interface AgentRound {
  index: number
  plan: AgentPlan
  intents: AgentCaseIntent[]
  notes: string[]
  result: unknown
  evidence: AgentEvidence[]
  diagnosis: AgentDiagnosis | null
  adaptation: AgentAdaptation | null
}

/** 编排节点时间线事件；sequence 排序，status 与 reason 解释该节点的观察状态。 */
export interface AgentEvent {
  sequence: number
  timestamp: string
  node: string
  status: string
  reason: string
}

/** 单次模型调用记录；usage_reported 区分真实用量与未报告用量，错误不由 token 数推断。 */
export interface AgentModelCall {
  stage: string
  status: string
  model: string
  error_kind: string | null
  prompt_tokens: number
  completion_tokens: number
  usage_reported: boolean
  duration_ms: number
}

/** 单类资源的预算汇总，分开表示限额、预留、消耗和不确定观测。 */
export interface AgentBudgetLine {
  limit: number
  reserved: number
  consumed: number
  released?: number
  observed_attempts?: number
  unknown_requests?: number
}

/** 编排运行完整快照；执行 status、报告完整度及模型实际状态分别表示不同维度。 */
export interface AgentRun extends ResultMetadata {
  schema_version: string
  id: string
  created_at: string
  status: string
  report_status: 'partial' | 'complete'
  stop_reason: string | null
  snapshot: Record<string, unknown>
  snapshot_sha256: string
  actual_llm_status: string
  degradation_reasons: string[]
  rounds: AgentRound[]
  events: AgentEvent[]
  model_calls: AgentModelCall[]
  commentary: { overview: string; recommendations: string[] } | null
  budget: { target_http: AgentBudgetLine; model_call: AgentBudgetLine }
  started_at?: string
  ended_at?: string
  duration_ms?: number
}

/** 编排启动输入；scope 必须来自人工确认，预算与补测变更权限显式传入，省略项由后端采用默认值。 */
export interface AgentRunInput {
  contract: string | Record<string, unknown>
  scope: {
    origin: string
    allowed_methods: string[]
    path_templates: string[]
    auth_ref?: unknown
  }
  max_cases?: number
  limits?: {
    target_requests?: number
    model_calls?: number
    max_rounds?: number
    wall_time_seconds?: number
  }
  engine?: string
  allow_mutating_followups?: boolean
}
