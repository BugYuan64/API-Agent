/** 执行结果的展示边界：区分本地结果码、目标真实 HTTP 和未评估断言。
 * 依赖后端追加的结果字段，同时兼容旧历史；只生成文案与色调，不改变执行或重试策略。 */

/** 兼容控制台、计划、长连接与旧历史的最小结果输入。 */
export interface ResultLike {
  result_code?: string
  result_status?: number | null
  status?: string
  error_kind?: string | null
  ok?: boolean
}

/** 由稳定结果码生成的只读展示值；resultStatus 永远不是目标 HTTP 状态。 */
export interface ResultPresentation {
  code: string
  title: string
  message: string
  tone: 'success' | 'warning' | 'danger' | 'neutral' | 'info'
  resultStatus: number | null
}

const LABELS: Record<string, [string, ResultPresentation['tone'], string, number | null]> = {
  PASSED: ['测试通过', 'success', '已取得执行证据，全部断言通过。', 200],
  HTTP_RESPONSE: ['收到 HTTP 响应', 'success', '已收到目标响应；业务是否成功仍需结合接口契约判断。', 200],
  ASSERTION_FAILED: ['断言未通过', 'warning', '实际响应与测试期望不符；也需核对期望本身是否正确。', 422],
  REQUEST_TIMEOUT: ['请求超时', 'warning', '目标未在请求时限内返回完整响应；重试前请确认是否已产生副作用。', 504],
  EXECUTION_TIMEOUT: ['执行超时', 'warning', '整体执行超过时限；已完成结果保留，其余结果仍需确认。', 504],
  MODEL_TIMEOUT: ['模型调用超时', 'warning', '模型未在时限内完成返回，已有测试证据仍然保留。', 504],
  MODEL_ERROR: ['模型结果不可用', 'warning', '模型调用失败或输出未通过校验，请查看已有测试证据。', 502],
  MODEL_AUTH_FAILED: ['模型鉴权失败', 'danger', '请检查模型供应方的密钥与访问权限。', 502],
  BROWSER_TIMEOUT: ['页面等待超时', 'warning', '本页面等待 API-Agent 超时，后端可能仍在运行，请先查看历史结果。', null],
  MANAGEMENT_CONNECTION_FAILED: ['无法连接 API-Agent', 'danger', '请检查 API-Agent 服务和浏览器网络；目标执行结果仍需通过历史确认。', null],
  MANAGEMENT_ERROR: ['管理请求未完成', 'danger', 'API-Agent 未返回有效执行结果，请检查请求配置和服务状态。', null],
  CANCELLED: ['已取消', 'neutral', '本次等待或执行已停止；不能据此证明目标操作已撤销。', null],
  UNKNOWN_RESULT: ['结果未知', 'neutral', '缺少足够执行证据，不能视为通过或已确认失败。', null],
  CONNECTION_FAILED: ['连接失败', 'danger', '未取得目标响应，请核对服务地址、端口及网络连接。', 502],
  TLS_ERROR: ['TLS 连接失败', 'danger', '安全连接或证书验证失败，未取得可评估的目标响应。', 502],
  RESPONSE_LIMIT_EXCEEDED: ['响应超过大小限制', 'warning', '读取已按字节预算中止，相关断言未评估。', 413],
  RESPONSE_ENCODING_ERROR: ['响应无法解码', 'danger', '响应编码不受支持或正文不完整，相关断言未评估。', 502],
  INVALID_REPORT: ['执行证据不完整', 'warning', '执行器报告缺失或不符合契约，不能据此判定测试通过。', 502],
  ENGINE_UNAVAILABLE: ['执行引擎不可用', 'danger', '请检查引擎配置与本机依赖，未取得有效执行结果。', 503],
  POLICY_REJECTED: ['请求被策略拒绝', 'warning', '请检查目标范围、凭据引用与请求配置。', 400],
  BUDGET_EXHAUSTED: ['达到运行预算', 'warning', '已达到调用、轮次或并发上限，请检查本次运行限制。', 429],
  EXECUTION_ERROR: ['执行异常', 'danger', '执行未能正常完成，请结合错误类别和已有证据排查。', 500],
  RUNNING: ['运行中', 'info', '尚未取得最终结果。', null],
  COMPLETED: ['流程已完成', 'info', '流程已结束；测试是否通过请查看逐项断言与保存的证据。', 200],
  INTERRUPTED: ['已中断', 'warning', '进程重启或执行中断，已保存证据可查看；不会自动重发目标请求。', null],
  LIMIT_REACHED: ['达到观察上限', 'neutral', '已按事件或字节预算停止观察。', null],
  HANDSHAKE_REJECTED: ['握手被拒绝', 'warning', '目标拒绝建立长连接，请核对地址、协议与鉴权要求。', 502],
}

/** 为没有新增结果字段的旧历史恢复展示码，保留 error_kind 的优先级。 */
function legacyCode(value: ResultLike): string {
  const error = value.error_kind ?? ''
  if (error === 'request_timeout' || error === 'connect_timeout') return 'REQUEST_TIMEOUT'
  if (error === 'execution_timeout' || error === 'deadline_exceeded' || error === 'wall_time_exhausted') return 'EXECUTION_TIMEOUT'
  if (error === 'model_timeout') return 'MODEL_TIMEOUT'
  if (error === 'model_authentication_failed') return 'MODEL_AUTH_FAILED'
  if (error === 'model_response_error' || error === 'invalid_model_output') return 'MODEL_ERROR'
  if (error === 'handshake_rejected') return 'HANDSHAKE_REJECTED'
  if (error === 'tls_error') return 'TLS_ERROR'
  if (['request_error', 'request_failed', 'connect_failed', 'connection_failed'].includes(error)) return 'CONNECTION_FAILED'
  if (error === 'response_limit_exceeded') return 'RESPONSE_LIMIT_EXCEEDED'
  if (error.startsWith('response_encod') || error === 'response_decode_error' || error === 'unsupported_content_encoding') return 'RESPONSE_ENCODING_ERROR'
  if (error === 'cli_unavailable' || error === 'unknown_engine') return 'ENGINE_UNAVAILABLE'
  if (/report|assertions/.test(error)) return 'INVALID_REPORT'
  if (error === 'cancelled' || value.status === 'cancelled') return 'CANCELLED'
  if (error) return 'EXECUTION_ERROR'
  if (value.ok === true) return 'HTTP_RESPONSE'
  if (value.status === 'passed') return 'PASSED'
  if (value.status === 'failed') return 'ASSERTION_FAILED'
  if (value.status === 'timed_out') return 'EXECUTION_TIMEOUT'
  if (value.status === 'completed' || value.status === 'closed') return 'COMPLETED'
  if (value.status === 'interrupted') return 'INTERRUPTED'
  if (['queued', 'running', 'connecting', 'open', 'cancelling'].includes(value.status ?? '')) return 'RUNNING'
  if (value.status === 'error' || value.ok === false) return 'EXECUTION_ERROR'
  return 'UNKNOWN_RESULT'
}

/** 将结果及可选的真实目标 HTTP 转成展示值；未知新码明确展示，绝不静默转为成功。 */
export function presentResult(value: ResultLike, targetHttp?: number | null): ResultPresentation {
  const code = value.result_code || legacyCode(value)
  const entry = LABELS[code]
  if (!entry) return { code, title: `未识别的结果（${code}）`, message: '请保留原始结果码供后续排查。', tone: 'neutral', resultStatus: value.result_status ?? null }
  const [title, tone, message, fallbackStatus] = entry
  const shown: ResultPresentation = { code, title, tone, message,
    resultStatus: value.result_status === undefined ? fallbackStatus : value.result_status }
  if (code === 'HTTP_RESPONSE' && targetHttp != null) {
    shown.title = `目标返回 HTTP ${targetHttp}`
    // 与 format.ts 的 statusTone 保持同一套区间语义（1xx/非法值 → neutral），
    // 此处内联而不 import：results.ts 要保持零依赖以便单文件转译测试。
    shown.tone =
      targetHttp >= 200 && targetHttp < 300 ? 'success'
      : targetHttp >= 300 && targetHttp < 400 ? 'info'
      : targetHttp >= 400 && targetHttp < 500 ? 'warning'
      : targetHttp >= 500 ? 'danger'
      : 'neutral'
  }
  return shown
}

/**
 * 本项目语义码 → 带前缀的标签。
 *
 * 作用：`resultStatus` 是结果分类码（如 422 表示 ASSERTION_FAILED），
 * 与目标真实 HTTP 状态码长得一样却含义不同。裸数字并排展示时极易误读，
 * 因此所有展示点统一加「结果码」前缀。
 */
export function resultCodeLabel(resultStatus: number | null | undefined): string | null {
  return resultStatus == null ? null : `结果码 ${resultStatus}`
}

type StatusFeedback = { label: string; tone: ResultPresentation['tone']; symbol: string }

/** 三态断言展示：null/缺失表示未评估，只有明确 false 才显示未通过。 */
export function assertionVerdict(passed: boolean | null | undefined): StatusFeedback {
  if (passed === true) return { label: '通过', symbol: '✓', tone: 'success' }
  if (passed === false) return { label: '未通过', symbol: '✕', tone: 'danger' }
  return { label: '未评估', symbol: '—', tone: 'neutral' }
}

const CASE_OUTCOMES = new Map<string, StatusFeedback>([
  ['passed', { label: '通过', tone: 'success', symbol: '✓' }],
  ['failed', { label: '未通过', tone: 'danger', symbol: '✕' }],
  ['error', { label: '执行错误', tone: 'danger', symbol: '!' }],
  ['timed_out', { label: '超时', tone: 'warning', symbol: '◷' }],
  ['skipped', { label: '已跳过', tone: 'warning', symbol: '↷' }],
  ['permission_required', { label: '待授权', tone: 'warning', symbol: '↷' }],
  ['cancelled', { label: '已取消', tone: 'neutral', symbol: '—' }],
  ['unknown', { label: '未评估', tone: 'neutral', symbol: '—' }],
])

/** Map permission skips in existing task history to the same verdict as new runs. */
function caseOutcomeKey(status: string, reason?: string) {
  return status === 'skipped' && reason === 'write_permission_required' ? 'permission_required' : status
}

export function caseVerdict(status: string, reason?: string): StatusFeedback {
  return CASE_OUTCOMES.get(caseOutcomeKey(status, reason)) ?? assertionVerdict(null)
}

export function countCaseOutcomes(results: readonly { status: string; reason?: string }[]) {
  const counts = new Map<string, number>()
  for (const result of results) {
    const status = caseOutcomeKey(result.status, result.reason)
    const key = CASE_OUTCOMES.has(status) ? status : 'unknown'
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return Array.from(CASE_OUTCOMES, ([status, feedback]) => ({ status, ...feedback, count: counts.get(status) ?? 0 }))
}

const TASK_PROGRESS = new Map<string, StatusFeedback>([
  ['queued', { label: '排队中', tone: 'info', symbol: '◷' }],
  ['running', { label: '执行中', tone: 'info', symbol: '▶' }],
  ['completed', { label: '流程完成', tone: 'info', symbol: '■' }],
  ['partial', { label: '部分完成', tone: 'warning', symbol: '!' }],
  ['cancelled', { label: '已取消', tone: 'neutral', symbol: '—' }],
  ['timed_out', { label: '已超时', tone: 'warning', symbol: '◷' }],
  ['interrupted', { label: '已中断', tone: 'warning', symbol: '!' }],
  ['failed', { label: '任务失败', tone: 'danger', symbol: '✕' }],
])

export function taskProgress(status: string): StatusFeedback {
  return TASK_PROGRESS.get(status) ?? { label: status, tone: 'neutral', symbol: '—' }
}

/** 管理链路失败独立展示；浏览器未收到结果不等于目标请求超时，不生成目标 HTTP。 */
export function presentManagementError(code: string, status: number): ResultPresentation {
  const resultCode = code === 'client_timeout' ? 'BROWSER_TIMEOUT'
    : code === 'request_cancelled' ? 'CANCELLED'
    : code === 'network_error' ? 'MANAGEMENT_CONNECTION_FAILED'
    : status === 400 || status === 422 ? 'POLICY_REJECTED'
    : 'MANAGEMENT_ERROR'
  return presentResult({ result_code: resultCode, result_status: status || null })
}
