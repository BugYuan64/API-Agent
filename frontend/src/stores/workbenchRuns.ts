/** 跨页面切换保留的工作台运行句柄与只读回放状态：按 PageScope 隔离，切页不丢后台任务或选中上下文。 */
import { defineStore } from 'pinia'
import { reactive } from 'vue'

/** 四个测试工作台的规范作用域标识。 */
export type PageScope = 'console' | 'websocket' | 'tasks' | 'model_testing'

export const PAGE_SCOPES: readonly PageScope[] = [
  'console',
  'websocket',
  'tasks',
  'model_testing',
] as const

/** 点击 AI 回复中的用例超链接后进入的只读回放目标。 */
export interface ReadonlyReplayState {
  historyId: string
  caseId: string
  round?: number
  label?: string
}

function createScopeMap<T>(initial: T): Record<PageScope, T> {
  return {
    console: initial,
    websocket: initial,
    tasks: initial,
    model_testing: initial,
  }
}

function normalizeId(value: string | null | undefined): string | null {
  if (!value) return null
  const trimmed = value.trim()
  return trimmed ? trimmed : null
}

/** 统一保存四页后台运行 ID、历史选中 ID、助手运行 ID 与只读回放上下文。 */
export const useWorkbenchRunsStore = defineStore('workbenchRuns', () => {
  const activeRunId = reactive<Record<PageScope, string | null>>(createScopeMap<string | null>(null))
  const activeHistoryId = reactive<Record<PageScope, string | null>>(createScopeMap<string | null>(null))
  const activeAssistantRunId = reactive<Record<PageScope, string | null>>(createScopeMap<string | null>(null))
  const readonlyReplay = reactive<Record<PageScope, ReadonlyReplayState | null>>(
    createScopeMap<ReadonlyReplayState | null>(null),
  )

  function setActiveRun(scope: PageScope, runId: string | null): void {
    activeRunId[scope] = normalizeId(runId)
  }

  function setActiveHistory(scope: PageScope, historyId: string | null): void {
    activeHistoryId[scope] = normalizeId(historyId)
  }

  function setActiveAssistantRun(scope: PageScope, runId: string | null): void {
    activeAssistantRunId[scope] = normalizeId(runId)
  }

  function enterReadonlyReplay(scope: PageScope, payload: ReadonlyReplayState): void {
    readonlyReplay[scope] = { ...payload }
  }

  function exitReadonlyReplay(scope: PageScope): void {
    readonlyReplay[scope] = null
  }

  function resetScope(scope: PageScope): void {
    activeRunId[scope] = null
    activeHistoryId[scope] = null
    activeAssistantRunId[scope] = null
    readonlyReplay[scope] = null
  }

  return {
    activeRunId,
    activeHistoryId,
    activeAssistantRunId,
    readonlyReplay,
    setActiveRun,
    setActiveHistory,
    setActiveAssistantRun,
    enterReadonlyReplay,
    exitReadonlyReplay,
    resetScope,
  }
})
