/** Typed document-task API; transport failures use the shared management client. */
import { request } from './client'

export interface TestCase {
  name: string
  protocol: 'http' | 'sse' | 'websocket'
  method: string
  path: string
  source: string
  assertions: { kind: string; expected: unknown; pointer?: string }[]
  cases?: TestCase[]
  generated_cases?: TestCase[]
}
export interface Catalog {
  format: string
  endpoints: TestCase[]
  cases: TestCase[]
  warnings: string[]
}
export interface TaskRecord {
  id: string
  status: string
  stage: string
  created_at: string
  error_kind: string | null
  snapshot: { cases: TestCase[]; warnings: string[]; engine: string }
  results: { index: number; name: string; protocol: string; source: string; status: string; reason?: string;
    assertions: { kind: string; expected: unknown; actual: unknown; passed: boolean | null }[]; evidence?: unknown }[]
  summary: string | null
}
/** Recognize uploaded text; the backend decides between local parsing and LLM extraction. */
export const recognizeDocument = (filename: string, text: string) =>
  request<Catalog>('/api/tasks/recognize', { method: 'POST', body: { filename, text }, timeoutMs: 125000 })
/** Start a bounded task with an explicit target and write permission. */
export const startTask = (body: { catalog: Catalog; origin: string; allow_writes: boolean; observation_ms: number; wall_time_seconds: number }) =>
  request<TaskRecord>('/api/tasks', { method: 'POST', body })
/** Read saved progress and evidence. */
export const readTask = (id: string, signal?: AbortSignal) => request<TaskRecord>(
  '/api/tasks/' + encodeURIComponent(id), { signal })
/** List persisted task history after refresh. */
export const listTasks = () => request<{ items: TaskRecord[] }>('/api/tasks')
/** Stop a running task and retain partial evidence. */
export const cancelTask = (id: string) => request<TaskRecord>(
  '/api/tasks/' + encodeURIComponent(id) + '/cancel', { method: 'POST' })
/** Delete only terminal task artifacts. */
export const deleteTask = (id: string) => request<null>(
  '/api/tasks/' + encodeURIComponent(id), { method: 'DELETE' })
