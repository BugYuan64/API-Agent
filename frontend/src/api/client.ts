/** 管理 API 的浏览器传输边界：依赖 fetch、AbortController 和定时器，统一请求截止时间、响应解析与 ApiError。只封装管理接口，目标测试结果仍由后端响应描述。 */
/** 一个极薄的 fetch 封装：统一错误结构、超时与 JSON 解析。
 *
 *  为什么要封装：普通管理错误使用 {error: {code, message, details, request_id}}，
 *  如果让每个页面各自 parse，错误提示会各写一份且容易漏掉 request_id。
 *  这里把错误收敛成 ApiError；控制台可显式接收已分类的目标传输失败信封。 */

/** 管理链路统一异常，保存 HTTP 状态、业务码、字段详情及请求追踪 ID，供页面转换为可展示文案。 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: { field: string; reason: string }[]
  readonly requestId: string

  /** 保存管理接口状态、业务错误码、字段详情和 requestId；不解析响应或展示提示，缺省详情与请求 ID 为空。 */
  constructor(
    status: number,
    code: string,
    message: string,
    details: { field: string; reason: string }[] = [],
    requestId = '',
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
  }
}

/** 管理请求选项：body 按 JSON 序列化；signal 与 timeoutMs 任一触发均中止完整响应读取。 */
interface RequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
  timeoutMs?: number
  /** 仅控制台启用：保留后端已分类的传输失败结果，普通管理错误仍抛出。 */
  acceptResultErrors?: boolean
}

/** 识别控制台的失败结果信封，防止将任意非 2xx 管理错误当成测试结果。 */
function isResultError(value: unknown, status: number): boolean {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return item.ok === false && item.response === null && !!item.request &&
    typeof item.request === 'object' && typeof item.error_kind === 'string' &&
    typeof item.result_code === 'string' && item.result_status === status
}

/** 解析后端响应；无论成功失败都尝试读 JSON，读不出来就退回文本。 */
async function readBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

/** 将 path 和请求选项发送为管理 API 请求并返回类型化响应。调用方取消或截止时间覆盖响应体读取，非 2xx 和网络失败均抛 ApiError；finally 释放定时器和外部取消监听。 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, timeoutMs = 30_000, acceptResultErrors = false } = options
  if (signal?.aborted) throw new ApiError(0, 'request_cancelled', '请求已取消')
  const controller = new AbortController()
  let deadlineExpired = false
  // 外层 timeout 与调用方传入的 signal 合并：任一触发都中止请求。
  const timer = window.setTimeout(() => { deadlineExpired = true; controller.abort() }, timeoutMs)
  /** 仅描述浏览器到管理服务的等待终止，不伪造目标 HTTP 状态。 */
  const aborted = () => new ApiError(0, deadlineExpired ? 'client_timeout' : 'request_cancelled',
    deadlineExpired ? '本页面等待 API-Agent 超时，后端可能仍在运行，请先查看历史结果' : '请求已取消')
  /** 把调用方的中止通知转发给内部控制器；实际错误归一化及监听清理由 request 统一完成。 */
  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  try {
    const response = await fetch(path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
    // Headers alone do not complete a request: cancellation and the deadline
    // also cover streaming the body and parsing the response.
    const payload = await readBody(response)
    if (controller.signal.aborted) throw aborted()
    if (!response.ok) {
      if (acceptResultErrors && isResultError(payload, response.status)) return payload as T
      const shaped = payload as { error?: { code: string; message: string; details?: []; request_id?: string } } | null
      const detail = shaped?.error
      throw new ApiError(
        response.status,
        detail?.code ?? `http_${response.status}`,
        detail?.message ?? `请求失败（HTTP ${response.status}）`,
        detail?.details ?? [],
        detail?.request_id ?? '',
      )
    }
    return payload as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (controller.signal.aborted) {
      throw aborted()
    }
    throw new ApiError(0, 'network_error', (error as Error).message || '无法连接后端服务')
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }

}

/** 把任意异常转成一句可展示的中文说明，避免页面各自判断类型。 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const suffix = error.requestId ? `（request_id ${error.requestId.slice(0, 8)}）` : ''
    return `${error.message}${suffix}`
  }
  if (error instanceof Error) return error.message
  return String(error)
}
