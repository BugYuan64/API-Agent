/** 接口测试工作台内存状态：跨路由切换保留表单草稿、当前运行句柄、响应证据与 AI 子用例只读回放上下文。 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type {
  BodyMode,
  ConsoleHistoryDetail,
  ConsoleRequestInput,
  ConsoleResult,
  ConsoleRunSummary,
  ConsoleSseEvent,
  ConsoleSubCase,
  HttpMethod,
} from '@/api/types'
import type { HeaderRow } from '@/composables/useHeaderRows'
import type { ResultPresentation } from '@/lib/results'
import type { ReadonlyReplayState } from '@/stores/workbenchRuns'

export type ConsoleTab = 'params' | 'headers' | 'body' | 'meta'

export type ConsoleDetailRequest =
  | ConsoleRequestInput
  | Pick<ConsoleRequestInput, 'url' | 'method' | 'headers' | 'body_mode' | 'body'>

export interface ConsoleWorkbenchSnapshot {
  method: HttpMethod
  url: string
  prefix: string
  bodyMode: BodyMode
  body: string
  headers: HeaderRow[]
  tab: ConsoleTab
  activeHistoryId: string | null
  activeRunId: string | null
  activeRunProgress: ConsoleRunSummary | null
  result: ConsoleResult | null
  liveEvents: ConsoleSseEvent[] | null
  localFailure: ResultPresentation | null
  responseCleared: boolean
  detailRequest: ConsoleDetailRequest | null
  activeDetail: ConsoleHistoryDetail | null
  liveSubCases: ConsoleSubCase[]
  readonlyReplay: ReadonlyReplayState | null
  activeSubCase: ConsoleSubCase | null
  draftVersion: number
}

const DEFAULT_FORM = {
  method: 'GET' as HttpMethod,
  url: 'http://127.0.0.1:8010/health',
  prefix: '',
  bodyMode: 'none' as BodyMode,
  body: '',
}

function cloneHeaderRows(rows: readonly HeaderRow[] | null | undefined): HeaderRow[] {
  if (!rows || rows.length === 0) return [{ name: '', value: '' }]
  return rows.map((row) => ({ name: row.name, value: row.value }))
}

/** 在切换至其他测试页并返回时保持接口测试工作台的表单、运行进度与回放状态不丢失。 */
export const useConsoleWorkbenchStore = defineStore('consoleWorkbench', () => {
  const hydrated = ref(false)
  const form = reactive({
    method: DEFAULT_FORM.method as HttpMethod,
    url: DEFAULT_FORM.url,
    prefix: DEFAULT_FORM.prefix,
    bodyMode: DEFAULT_FORM.bodyMode as BodyMode,
    body: DEFAULT_FORM.body,
  })
  const headers = ref<HeaderRow[]>([{ name: '', value: '' }])
  const tab = ref<ConsoleTab>('body')
  const activeHistoryId = ref<string | null>(null)
  const activeRunId = ref<string | null>(null)
  const activeRunProgress = ref<ConsoleRunSummary | null>(null)
  const result = ref<ConsoleResult | null>(null)
  const liveEvents = ref<ConsoleSseEvent[] | null>(null)
  const localFailure = ref<ResultPresentation | null>(null)
  const responseCleared = ref(false)
  const detailRequest = ref<ConsoleDetailRequest | null>(null)
  const activeDetail = ref<ConsoleHistoryDetail | null>(null)
  const liveSubCases = ref<ConsoleSubCase[]>([])
  const readonlyReplay = ref<ReadonlyReplayState | null>(null)
  const activeSubCase = ref<ConsoleSubCase | null>(null)
  const draftVersion = ref(0)

  /** 将页面当前表单、响应与只读回放上下文整体写入内存仓库。 */
  function saveSnapshot(snapshot: ConsoleWorkbenchSnapshot): void {
    form.method = snapshot.method
    form.url = snapshot.url
    form.prefix = snapshot.prefix
    form.bodyMode = snapshot.bodyMode
    form.body = snapshot.body
    headers.value = cloneHeaderRows(snapshot.headers)
    tab.value = snapshot.tab
    activeHistoryId.value = snapshot.activeHistoryId
    activeRunId.value = snapshot.activeRunId
    activeRunProgress.value = snapshot.activeRunProgress ? { ...snapshot.activeRunProgress } : null
    result.value = snapshot.result
    liveEvents.value = snapshot.liveEvents ? snapshot.liveEvents.map((evt) => ({ ...evt })) : null
    localFailure.value = snapshot.localFailure ? { ...snapshot.localFailure } : null
    responseCleared.value = snapshot.responseCleared
    detailRequest.value = snapshot.detailRequest ? { ...snapshot.detailRequest } : null
    activeDetail.value = snapshot.activeDetail
    liveSubCases.value = snapshot.liveSubCases.slice()
    readonlyReplay.value = snapshot.readonlyReplay ? { ...snapshot.readonlyReplay } : null
    activeSubCase.value = snapshot.activeSubCase
    draftVersion.value = snapshot.draftVersion
    hydrated.value = true
  }

  function setActiveRun(runId: string | null, progress?: ConsoleRunSummary | null): void {
    activeRunId.value = runId
    if (progress !== undefined) {
      activeRunProgress.value = progress ? { ...progress } : null
    } else if (!runId) {
      activeRunProgress.value = null
    }
    hydrated.value = true
  }

  function setActiveHistory(historyId: string | null): void {
    activeHistoryId.value = historyId
    hydrated.value = true
  }

  function enterReadonlyReplay(replay: ReadonlyReplayState, subCase: ConsoleSubCase | null): void {
    readonlyReplay.value = { ...replay }
    activeSubCase.value = subCase
    hydrated.value = true
  }

  function exitReadonlyReplay(): void {
    readonlyReplay.value = null
    activeSubCase.value = null
    hydrated.value = true
  }

  function clearResponse(): void {
    responseCleared.value = true
    detailRequest.value = null
    result.value = null
    liveEvents.value = null
    localFailure.value = null
    hydrated.value = true
  }

  /** 新建空白测试时彻底重置表单、历史绑定、回放锁与响应显示。 */
  function resetForNewTest(nextDraftVersion?: number): void {
    form.method = 'GET'
    form.url = ''
    form.prefix = ''
    form.bodyMode = 'none'
    form.body = ''
    headers.value = [{ name: '', value: '' }]
    tab.value = 'body'
    activeHistoryId.value = null
    activeRunId.value = null
    activeRunProgress.value = null
    result.value = null
    liveEvents.value = null
    localFailure.value = null
    responseCleared.value = true
    detailRequest.value = null
    activeDetail.value = null
    liveSubCases.value = []
    readonlyReplay.value = null
    activeSubCase.value = null
    if (typeof nextDraftVersion === 'number') {
      draftVersion.value = nextDraftVersion
    } else {
      draftVersion.value++
    }
    hydrated.value = true
  }

  /** 再次测试时保留已填入的请求参数并解锁编辑，同时清空历史绑定、响应与只读锁。 */
  function resetForRetest(nextDraftVersion?: number): void {
    activeHistoryId.value = null
    activeRunId.value = null
    activeRunProgress.value = null
    result.value = null
    liveEvents.value = null
    localFailure.value = null
    responseCleared.value = false
    detailRequest.value = null
    activeDetail.value = null
    liveSubCases.value = []
    readonlyReplay.value = null
    activeSubCase.value = null
    if (typeof nextDraftVersion === 'number') {
      draftVersion.value = nextDraftVersion
    } else {
      draftVersion.value++
    }
    hydrated.value = true
  }

  return {
    hydrated,
    form,
    headers,
    tab,
    activeHistoryId,
    activeRunId,
    activeRunProgress,
    result,
    liveEvents,
    localFailure,
    responseCleared,
    detailRequest,
    activeDetail,
    liveSubCases,
    readonlyReplay,
    activeSubCase,
    draftVersion,
    saveSnapshot,
    setActiveRun,
    setActiveHistory,
    enterReadonlyReplay,
    exitReadonlyReplay,
    clearResponse,
    resetForNewTest,
    resetForRetest,
  }
})
