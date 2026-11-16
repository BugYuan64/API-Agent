/** 管理 API 与页面之间的数据契约：集中声明输入、分页、执行证据及编排结果。仅提供静态类型，不执行数据校验；可空字段及可选字段均需由消费页面处理。 */
/** 与后端管理 API 一一对应的类型。
 *  可空字段允许值为 null，可选字段允许省略；两者含义不同。
 *  类型声明不执行运行时校验，页面需显式处理历史记录与缺失值。 */

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
  sse?: { events: { event: string; data: string }[]; stop_reason: string } | null
}

/** 目标请求结果：ok 描述传输结果，HTTP 状态仍需单独查看；失败时 response 可空。 */
export interface ConsoleResult extends ResultMetadata {
  ok: boolean
  request: { method: string; url: string }
  response: ConsoleResponse | null
  error_kind: string | null
  error_message: string | null
  elapsed_ms: number
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
export type StreamStatus = 'connecting' | 'open' | 'closed' | 'error' | 'cancelled'

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
}

/* ---------- 计划执行 ---------- */

/** 单条断言的确定性结果；passed 为 null 表示没有可确认的通过或失败结论。 */
export interface AssertionResult {
  index: number
  kind: string
  passed: boolean | null
  message: string | null
}

/** 单个请求用例的执行观测及断言；未知请求和异常必须保留，不能自动当作断言失败。 */
export interface CaseResult extends ResultMetadata {
  id: string
  status: 'passed' | 'failed' | 'timed_out' | 'cancelled' | 'error' | 'unknown'
  http_status: number | null
  response_time_ms: number | null
  error_kind: string | null
  assertions: AssertionResult[]
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
