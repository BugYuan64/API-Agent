/** Browser-local transcripts for real test executions, including pre-existing links. */
import type { CaseLinkBlock, CaseLinkTone, PageScope } from '@/api/types'

const VALID_SCOPES: ReadonlySet<string> = new Set<PageScope>([
  'console',
  'websocket',
  'tasks',
  'model_testing',
])

const VALID_TONES: ReadonlySet<string> = new Set<CaseLinkTone>([
  'success',
  'danger',
  'warning',
  'info',
  'neutral',
])

const transcriptPrefix = (scope: string) => `api-agent.chat.${scope}.`
const executionPrefix = (scope: string) => `api-agent.chat-execution.${scope}.`

export function chatTranscriptKey(scope: string, executionId: string): string {
  let transcriptId = executionId
  try {
    transcriptId = window.localStorage.getItem(`${executionPrefix(scope)}${executionId}`) ?? executionId
  } catch {
    // Use the direct key if storage is unavailable.
  }
  return `${transcriptPrefix(scope)}${transcriptId}`
}

export function removeTestChat(scope: string, executionId: string): void {
  try {
    window.localStorage.removeItem(chatTranscriptKey(scope, executionId))
    window.localStorage.removeItem(`${executionPrefix(scope)}${executionId}`)
  } catch {
    // Storage failure must not undo deletion of the test itself.
  }
}

/** Only called after explicitly clearing all tests for this scope. */
export function clearTestChats(scope: string): void {
  try {
    const storage = window.localStorage
    for (let index = storage.length - 1; index >= 0; index--) {
      const key = storage.key(index)
      if (key?.startsWith(transcriptPrefix(scope)) || key?.startsWith(executionPrefix(scope))) storage.removeItem(key)
    }
    storage.removeItem(`api-agent.chat-history.${scope}`)
  } catch {
    // Keep the execution history clear even if browser storage is blocked.
  }
}

function normalizeTone(raw: unknown): CaseLinkTone {
  if (typeof raw !== 'string') return 'neutral'
  const lowered = raw.trim().toLowerCase()
  if (VALID_TONES.has(lowered)) return lowered as CaseLinkTone
  if (lowered === 'error' || lowered === 'failed' || lowered === 'fail') return 'danger'
  if (lowered === 'ok' || lowered === 'passed' || lowered === 'pass' || lowered === 'available') return 'success'
  if (lowered === 'warn' || lowered === 'partial' || lowered === 'cancelled') return 'warning'
  return 'neutral'
}

/** 校验并规范化来自后端流或持久化线程的单条 CaseLinkBlock；非法项返回 null。 */
export function normalizeCaseLinkBlock(value: unknown): CaseLinkBlock | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  if (raw.type !== 'case_link') return null
  if (typeof raw.scope !== 'string' || !VALID_SCOPES.has(raw.scope)) return null
  if (typeof raw.history_id !== 'string' || !raw.history_id.trim()) return null
  if (typeof raw.case_id !== 'string' || !raw.case_id.trim()) return null

  const historyId = raw.history_id.trim()
  const caseId = raw.case_id.trim()
  const rawRound = typeof raw.round === 'number' ? raw.round : Number(raw.round)
  const round = Number.isFinite(rawRound) && rawRound >= 1 ? Math.trunc(rawRound) : 1
  const label = typeof raw.label === 'string' && raw.label.trim() ? raw.label.trim() : caseId
  const status = typeof raw.status === 'string' && raw.status.trim() ? raw.status.trim() : 'completed'

  return {
    type: 'case_link',
    scope: raw.scope as PageScope,
    history_id: historyId,
    case_id: caseId,
    round,
    label,
    status,
    tone: normalizeTone(raw.tone),
  }
}

/** 批量过滤并规范化 CaseLinkBlock 列表。 */
export function normalizeCaseLinkBlocks(values: unknown): CaseLinkBlock[] {
  if (!Array.isArray(values)) return []
  const result: CaseLinkBlock[] = []
  for (const item of values) {
    const normalized = normalizeCaseLinkBlock(item)
    if (normalized) result.push(normalized)
  }
  return result
}

/** 按 (scope, history_id, round, case_id) 去重 CaseLinkBlock，保留首次出现顺序并采用最新状态。 */
export function dedupeCaseLinkBlocks(blocks: CaseLinkBlock[]): CaseLinkBlock[] {
  const map = new Map<string, CaseLinkBlock>()
  for (const block of blocks) {
    const key = `${block.scope}:${block.history_id}:${block.round}:${block.case_id}`
    map.set(key, block)
  }
  return [...map.values()]
}
