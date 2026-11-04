/** 与后端管理 API 一一对应的类型。
 *  类型只描述「响应里确定存在的字段」；可选字段对应后端允许为 null 的部分，
 *  这样界面在渲染时必须显式处理缺失，而不是被 any 掩盖。 */

export type HttpMethod = 'GET' | 'HEAD' | 'OPTIONS' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
export type BodyMode = 'none' | 'json' | 'text' | 'form'
export type StreamKind = 'sse' | 'websocket'

/** 后端统一错误结构：所有非 2xx 都是这个形状（含框架级 404）。 */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
    details: { field: string; reason: string }[]
    request_id: string
  }
}

export interface PageInfo {
  limit: number
  offset: number
  total: number
}

export interface Paged<T> {
  items: T[]
  page: PageInfo
}

export interface Health {
  status: string
  service: string
  version: string
  llm_mode: string
}

/* ---------- 项目与环境 ---------- */

export interface Project {
  id: string
  name: string
  description: string
  created_at: string
}

export interface AuthReference {
  kind: 'bearer' | 'api_key' | 'header'
  env_var: string
  header_name: string | null
}

export interface ReplayPolicy {
  allow_replay: boolean
  allow_non_idempotent: boolean
}

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

export interface Environment {
  id: string
  project_id: string
  name: string
  current_version: number
  created_at: string
  config: EnvironmentVersion
}

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

/* ---------- 模拟运行（阶段 1 已有能力） ---------- */

export type RunStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'


export interface Run {
  id: string
  snapshot_id: string
  run_mode: string
  configured_llm_mode: string
  actual_llm_status: string
  degradation_reason: string | null
  status: RunStatus
  stop_reason: string | null
  cancel_requested_at: string | null
  deadline: string | null
  started_at: string | null
  ended_at: string | null
  created_at: string
  cleanup_status: string
  workflow_error: string | null
  current_action: number
  action_count: number
}

export interface RunEvent {
  id: string
  run_id: string
  sequence: number
  node: string
  event_type: string
  status: string
  tool_name: string | null
  input_summary: Record<string, unknown>
  observation_summary: Record<string, unknown>
  decision_reason: string | null
  evidence_ids: string[]
  budget_summary: Record<string, unknown>
  created_at: string
}

export interface BudgetEntry {
  id: string
  run_id: string
  resource: string
  action: string
  amount: number
  status: string
  related_id: string | null
  created_at: string
}

export interface DispatchAttempt {
  id: string
  run_id: string
  kind: string
  sequence: number
  status: string
  error_kind: string | null
  started_at: string | null
  ended_at: string | null
  created_at: string
}

/* ---------- 控制台请求 ---------- */

export interface ConsoleRequestInput {
  method: HttpMethod
  url: string
  headers: Record<string, string>
  body_mode: BodyMode
  body?: string | null
  timeout_ms: number
  follow_redirects: boolean
}

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
  redirects: string[]
}

export interface ConsoleResult {
  ok: boolean
  request: { method: string; url: string }
  response: ConsoleResponse | null
  error_kind: string | null
  error_message: string | null
  elapsed_ms: number
}

export interface ConsoleHistory {
  id: string
  project_id: string | null
  method: string
  url: string
  request_headers: Record<string, unknown>
  body_mode: string
  body_preview: string | null
  ok: boolean
  status_code: number | null
  elapsed_ms: number
  error_kind: string | null
  error_message: string | null
  response_headers: Record<string, unknown>
  response_preview: string | null
  response_bytes: number
  created_at: string
}

/* ---------- 长连接 ---------- */

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

export type StreamStatus = 'connecting' | 'open' | 'closed' | 'error' | 'cancelled'

export interface StreamSession {
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
}

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

export interface StreamEventPage {
  session: StreamSession
  events: StreamEvent[]
  next_cursor: number
  finished: boolean
  has_more: boolean
}

export interface StreamHistory {
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
}

/* ---------- 计划执行 ---------- */

export interface AssertionResult {
  index: number
  kind: string
  passed: boolean | null
  message: string | null
}

export interface CaseResult {
  id: string
  status: 'passed' | 'failed' | 'error' | 'unknown'
  http_status: number | null
  response_time_ms: number | null
  error_kind: string | null
  assertions: AssertionResult[]
}

export interface Execution {
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

export interface LLMConfig {
  enabled: boolean
  provider: string
  base_url: string | null
  model: string
  timeout_seconds: number
  has_api_key: boolean
}

export interface LLMConfigUpdate {
  enabled: boolean
  provider?: string
  base_url: string | null
  api_key?: string | null
  model: string
  timeout_seconds?: number
}

export interface LLMPingResult {
  ok: boolean
  latency_ms: number
  message: string | null
}

export interface LLMModelListResult {
  ok: boolean
  models: string[]
  count: number
  message: string | null
}



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

export interface AgentAnalysis {
  summary: string
  hypotheses: AgentHypothesis[]
  coverage_gaps: string[]
}

export interface AgentDiagnosis {
  evidence: AgentEvidence[]
  analysis: AgentAnalysis
}

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

export interface AgentRequestCase {
  id: string
  method: string
  path: string
  query?: Record<string, string>
  body?: unknown
  assertions: { kind: string; pointer?: string; expected?: unknown }[]
}

export interface AgentCaseIntent {
  case_id: string
  category: 'positive' | 'boundary' | 'negative' | 'auth'
  basis: 'contract' | 'inferred'
  rationale: string
  source_quote: string | null
}

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

export interface AgentEvent {
  sequence: number
  timestamp: string
  node: string
  status: string
  reason: string
}

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

export interface AgentBudgetLine {
  limit: number
  reserved: number
  consumed: number
  released?: number
  observed_attempts?: number
  unknown_requests?: number
}

export interface AgentRun {
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
