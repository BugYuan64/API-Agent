/** Typed document-task API; transport failures use the shared management client. */
import { ApiError, request } from './client'
import type { ConsoleSseEvent } from './types'

export interface TaskDocument {
  filename: string
  text: string
}

export interface TestCase {
  name: string
  protocol: 'http' | 'sse' | 'websocket'
  method: string
  path: string
  path_template?: string | null
  source: string
  kind?: 'contract' | 'document' | 'test' | null
  headers?: Record<string, string>
  body?: unknown
  messages?: string[]
  assertions: { kind: string; expected: unknown; pointer?: string }[]
  cases?: TestCase[]
  generated_cases?: TestCase[]
}
export interface TaskSnapshot {
  origin?: string
  document?: Pick<TaskDocument, 'filename'>
  cases: TestCase[]
  operations?: { path: string; example_index: number; case_indices: number[] }[]
  warnings: string[]
  engine: string
}
export interface TaskCaseResult {
  index: number
  name: string
  protocol: string
  source: string
  status: string
  reason?: string
  assertions: { kind: string; pointer?: string; expected: unknown; actual: unknown; passed: boolean | null }[]
  evidence?: unknown
}
export interface Catalog {
  format: string
  title?: string | null
  server_url?: string | null
  endpoints: TestCase[]
  cases: TestCase[]
  warnings: string[]
}
/** Small history projection returned by the list endpoint. */
export interface TaskHistory {
  id: string
  title?: string | null
  status: string
  stage: string
  current_index?: number | null
  active_stream?: 'sse' | 'websocket' | null
  created_at: string
  error_kind: string | null
}
export interface TaskRecord extends TaskHistory {
  snapshot: TaskSnapshot
  results: TaskCaseResult[]
  summary: string | null
}
export interface TaskProgressFrame {
  type: 'progress'
  status: string
  stage: string
  current_index: number | null
  active_stream: TaskHistory['active_stream']
  error_kind: string | null
  results: TaskCaseResult[]
  events: ConsoleSseEvent[]
  events_seen: number
  summary: string | null
}
/** Keep the same title across both history surfaces, including legacy tasks. */
export const taskHistoryLabel = (task: TaskHistory) => `task: ${task.title || task.id.slice(0, 8)}`
/** Recognize uploaded text; the backend decides between local parsing and LLM extraction. */
export const recognizeDocument = (document: TaskDocument, count: number, signal?: AbortSignal, previous?: Catalog) =>
  request<Catalog>('/api/tasks/recognize', { method: 'POST', body: { ...document, tests_per_url: count, previous }, timeoutMs: 390_000, signal })

/** Refill only checked operations; the existing draft is the source of reviewed inputs. */
export const regenerateTests = (catalog: Catalog, selected: number[], count: number, document?: TaskDocument, signal?: AbortSignal) =>
  request<Catalog>('/api/tasks/regenerate', { method: 'POST', body: { catalog, selected, tests_per_url: count, document }, timeoutMs: 250_000, signal })

/** Start a native folder picker on the backend host; null means the operator cancelled. */
export const generateProjectDocument = () => request<{ filename: string; text: string | null; partial: boolean } | null>(
  '/api/tasks/generate-document', { method: 'POST', timeoutMs: 480_000 })

/** Read the original attachment as text, including JSON documents. */
export const readTaskDocument = (id: string, signal?: AbortSignal) => request<string>(
  '/api/tasks/' + encodeURIComponent(id) + '/document', { signal, readResponse: response => response.text() })

export interface TaskChatMessage { role: 'user' | 'assistant'; content: string }

/** Follow an assistant reply and distinguish an old API server from model errors. */
export function chatWithAssistant(taskId: string | undefined, messages: TaskChatMessage[], onText: (text: string) => void) {
  return request<string>('/api/tasks/chat', {
    method: 'POST', body: { task_id: taskId ?? null, messages }, timeoutMs: 130_000,
    readResponse: async response => {
      const incomplete = () => new ApiError(0, 'incomplete_stream', 'AI 回复未完整返回')
      if (!response.body || !response.headers.get('content-type')?.startsWith('application/x-ndjson')) throw incomplete()
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let pending = ''
      try {
        while (true) {
          const { value, done } = await reader.read()
          if (done) throw incomplete()
          const lines = (pending + decoder.decode(value, { stream: true })).split('\n')
          pending = lines.pop() ?? ''
          for (const line of lines) {
            if (!line) continue
            const frame = JSON.parse(line) as { type: string; text?: string; code?: string }
            if (frame.type === 'error') throw new ApiError(502, frame.code ?? 'chat_failed', `AI 回复失败（${frame.code ?? '未知错误'}）`)
            if (frame.type === 'text') onText(frame.text ?? '')
            if (frame.type === 'done') return frame.text ?? ''
          }
        }
      } finally {
        await reader.cancel().catch(() => {})
        reader.releaseLock()
      }
    },
  }).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 405) {
      throw new ApiError(405, 'assistant_route_unavailable',
        '当前 API-Agent 后端不支持 AI 对话接口。请打开新版服务，或更新并重新启动当前后端。',
        error.details, error.requestId)
    }
    throw error
  })
}

/** Ask the model for a concise title after the first conversation turn. */
export const generateChatTitle = (question: string) =>
  request<{ title: string }>('/api/tasks/chat/title', { method: 'POST', body: { question }, timeoutMs: 40_000 })
type RunOptions = { origin: string; allow_writes: boolean; observation_ms: number; wall_time_seconds: number }
/** Start a bounded task with an explicit target and write permission. */
export const startTask = (body: RunOptions & { catalog: Catalog; document?: TaskDocument }) =>
  request<TaskRecord>('/api/tasks', { method: 'POST', body })
/** Start a new run from a saved snapshot, applying current options and any edited inputs. */
export const rerunTask = (id: string, body: RunOptions & { case_edits?: Record<number, TestCase> }) =>
  request<TaskRecord>('/api/tasks/' + encodeURIComponent(id) + '/rerun', { method: 'POST', body })
/** Read saved progress and evidence. */
export const readTask = (id: string, signal?: AbortSignal) => request<TaskRecord>(
  '/api/tasks/' + encodeURIComponent(id), { signal })

/** Read progress as records arrive. The first frame replays completed results after reconnect. */
export const followTask = (id: string, signal: AbortSignal, onFrame: (frame: TaskProgressFrame, first: boolean) => void) =>
  request<void>('/api/tasks/' + encodeURIComponent(id) + '/live', {
    signal, timeoutMs: 3_660_000,
    readResponse: async response => {
      const incomplete = () => new ApiError(0, 'incomplete_stream', '用例进度流提前结束')
      if (!response.body || !response.headers.get('content-type')?.startsWith('application/x-ndjson')) throw incomplete()
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let pending = ''
      let first = true
      try {
        while (true) {
          const { value, done } = await reader.read()
          if (done) throw incomplete()
          const lines = (pending + decoder.decode(value, { stream: true })).split('\n')
          pending = lines.pop() ?? ''
          for (const line of lines) {
            if (!line) continue
            const frame = JSON.parse(line) as TaskProgressFrame | { type: 'heartbeat' }
            if (frame.type !== 'progress') continue
            onFrame(frame, first)
            first = false
            if (!['queued', 'running'].includes(frame.status)) return
          }
        }
      } finally {
        await reader.cancel().catch(() => {})
        reader.releaseLock()
      }
    },
  })
/** List persisted task history after refresh. */
export const listTasks = () => request<{ items: TaskHistory[] }>('/api/tasks')
/** Stop a running task and retain partial evidence. */
export const cancelTask = (id: string) => request<TaskRecord>(
  '/api/tasks/' + encodeURIComponent(id) + '/cancel', { method: 'POST' })
/** Delete only terminal task artifacts. */
export const deleteTask = (id: string) => request<null>(
  '/api/tasks/' + encodeURIComponent(id), { method: 'DELETE' })
