/** Browser-local chat index and execution links; immutable test snapshots remain owned by their APIs. */
export interface ChatHistoryRecord {
  id: string
  title: string
  created_at: string
}

export function listChatHistories(scope: string): ChatHistoryRecord[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(`api-agent.chat-history.${scope}`) ?? '[]')
    if (!Array.isArray(value)) return []
    return value.filter((item): item is ChatHistoryRecord =>
      !!item && typeof item === 'object' &&
      typeof item.id === 'string' && typeof item.title === 'string' && typeof item.created_at === 'string',
    )
  } catch {
    return []
  }
}

export function saveChatHistory(scope: string, record: ChatHistoryRecord): void {
  const records = listChatHistories(scope).filter(item => item.id !== record.id)
  try {
    window.localStorage.setItem(`api-agent.chat-history.${scope}`, JSON.stringify([record, ...records].slice(0, 100)))
  } catch {
    // The assistant remains usable if browser storage is unavailable.
  }
}

export function removeChatHistory(scope: string, id: string): void {
  try {
    window.localStorage.setItem(`api-agent.chat-history.${scope}`, JSON.stringify(
      listChatHistories(scope).filter(item => item.id !== id),
    ))
    window.localStorage.removeItem(chatTranscriptKey(scope, id))
    window.localStorage.removeItem(`api-agent.chat-execution.${scope}.${id}`)
  } catch {
    // Existing history data remains untouched when storage is unavailable.
  }
}

/** Link the execution to the same transcript so an in-flight reply keeps its storage key. */
export function attachChatToExecution(scope: string, chatId: string, executionId: string): boolean {
  if (!isChatOnly(chatId) || !listChatHistories(scope).some(item => item.id === chatId)) return false
  try {
    window.localStorage.setItem(`api-agent.chat-execution.${scope}.${executionId}`, chatId)
    window.localStorage.setItem(`api-agent.chat-history.${scope}`, JSON.stringify(
      listChatHistories(scope).filter(item => item.id !== chatId),
    ))
    return true
  } catch {
    return false
  }
}

export function chatTranscriptKey(scope: string, id: string): string {
  let transcriptId = id
  try {
    transcriptId = window.localStorage.getItem(`api-agent.chat-execution.${scope}.${id}`) ?? id
  } catch {
    // Keep using the original key when storage is blocked.
  }
  return `api-agent.chat.${scope}.${transcriptId}`
}

export function hasExecutionChat(scope: string, id: string): boolean {
  try {
    return window.localStorage.getItem(`api-agent.chat-execution.${scope}.${id}`) !== null
  } catch {
    return false
  }
}

export function hasChatTranscript(scope: string, id: string): boolean {
  try {
    return window.localStorage.getItem(chatTranscriptKey(scope, id)) !== null
  } catch {
    return false
  }
}

export const isChatOnly = (id: string) => id.startsWith('chat-')

export function createChatHistoryId(): string {
  return `chat-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`
}

export function titleChatHistory(scope: string, id: string, title: string): ChatHistoryRecord | undefined {
  const record = listChatHistories(scope).find(item => item.id === id)
  if (!record) return
  const updated = { ...record, title: summarizeFirstQuestion(title) }
  saveChatHistory(scope, updated)
  return updated
}

export function summarizeFirstQuestion(question: string): string {
  const sentence = question.trim().replace(/\s+/g, ' ')
  return sentence.length > 48 ? `${sentence.slice(0, 47)}…` : sentence || 'AI 对话'
}
