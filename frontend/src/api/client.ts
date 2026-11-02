/** 一个极薄的 fetch 封装：统一错误结构、超时与 JSON 解析。
 *
 *  为什么要封装：后端的每个非 2xx 都是 {error: {code, message, details, request_id}}，
 *  如果让每个页面各自 parse，错误提示会各写一份且容易漏掉 request_id。
 *  这里把错误收敛成 ApiError，页面只需关心 code 与 message。 */

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: { field: string; reason: string }[]
  readonly requestId: string

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

interface RequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
  timeoutMs?: number
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

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, timeoutMs = 30_000 } = options
  const controller = new AbortController()
  // 外层 timeout 与调用方传入的 signal 合并：任一触发都中止请求。
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  let response: Response
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError(0, 'network_aborted', '请求已取消或超时')
    }
    throw new ApiError(0, 'network_error', (error as Error).message || '无法连接后端服务')
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }

  const payload = await readBody(response)
  if (!response.ok) {
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
