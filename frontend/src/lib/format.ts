/** 展示层格式化与状态翻译：使用浏览器日期和 JSON 标准能力，不发起请求或修改领域数据。缺失、非法值按各函数约定回退；未知状态保留原值，避免隐藏新服务端状态。 */
/** 展示层的小工具：时间、字节、时长、截断与高亮。
 *  都保持纯函数，方便在模板里直接用，也便于单测。 */

/** 带日期的完整时间，用于历史列表。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('zh-CN', { hour12: false })
}

/** 相对时间：刚刚 / 3 分钟前 / 2 小时前，超过一天回到绝对时间。 */
export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  const diff = Date.now() - date.getTime()
  if (Number.isNaN(diff)) return '—'
  if (diff < 5_000) return '刚刚'
  if (diff < 60_000) return `${Math.floor(diff / 1000)} 秒前`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  return formatDateTime(value)
}

/** 毫秒 → 人类可读：850ms / 1.24s / 2m 10s。 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return `${minutes}m ${seconds}s`
}

/** 字节 → B / KB / MB，保留一位小数。 */
export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes || bytes < 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

/** HTTP 状态码 → 语义色（2xx 绿、3xx 蓝、4xx 橙、5xx 红）。 */
export function statusTone(status: number | null | undefined): 'success' | 'info' | 'warning' | 'danger' | 'neutral' {
  if (status == null) return 'neutral'
  if (status >= 200 && status < 300) return 'success'
  if (status >= 300 && status < 400) return 'info'
  if (status >= 400 && status < 500) return 'warning'
  if (status >= 500) return 'danger'
  return 'neutral'
}

/** 一跳重定向 → 展示标签。只描述目标真实回包，不推断跳转结果。 */
export function describeRedirect(status: number, reason: string | null | undefined): string {
  return reason ? `${status} ${reason}` : String(status)
}

/** 把 JSON 文本美化；解析失败时原样返回，避免把非 JSON 内容吃掉。 */
export function prettyJson(text: string | null | undefined): string {
  if (!text) return ''
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/** 判断一段文本看起来像 JSON（用于决定是否提供「格式化」按钮）。 */
export function looksLikeJson(text: string | null | undefined): boolean {
  if (!text) return false
  const trimmed = text.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return false
  try {
    JSON.parse(trimmed)
    return true
  } catch {
    return false
  }
}

/** 长连接结束原因 → 中文说明。保留原始值作为回退，避免新原因被隐藏。 */
export function describeStopReason(reason: string | null | undefined): string {
  switch (reason) {
    case 'target_closed':
      return '目标主动关闭'
    case 'limits_exceeded':
      return '达到事件/字节上限'
    case 'deadline_exceeded':
      return '达到时长上限'
    case 'client_stopped':
      return '手动停止'
    case 'service_shutdown':
      return '服务关闭'
    case 'connect_failed':
      return '连接失败'
    case 'connect_timeout':
      return '连接超时'
    case 'handshake_rejected':
      return '握手被拒'
    case 'invalid_url':
      return '地址不合法'
    case 'stream_failed':
      return '读取中断'
    case 'recovered_after_restart':
      return '进程重启后中断'
    case null:
    case undefined:
    case '':
      return '—'
    default:
      return reason
  }
}

/** 执行结果 → 中文标签。 */
export function describeExecutionStatus(status: string): string {
  const labels: Record<string, string> = {
    passed: '通过',
    failed: '断言未通过',
    error: '执行异常',
    timed_out: '超时',
    cancelled: '已取消',
  }
  return labels[status] ?? status
}

/** 断言类型 → 中文说明，让表格里不再出现裸英文。 */
export function describeAssertionKind(kind: string): string {
  const labels: Record<string, string> = {
    status: '状态码',
    json_equals: 'JSON 相等',
    json_exists: 'JSON 字段存在',
    content_type: '内容类型',
    response_time: '响应时间',
  }
  return labels[kind] ?? kind
}
