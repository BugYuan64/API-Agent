/** 长连接（WebSocket）工作台跨页持久化状态：保存表单参数、活跃手动会话/AI 套件、事件时间线与只读回放上下文。 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type {
  StreamEvent,
  StreamSession,
  StreamSubCaseSummary,
  StreamSuiteStatus,
} from '@/api/types'
import type { HeaderRow } from '@/composables/useHeaderRows'

export const DEFAULT_WS_URL = 'ws://127.0.0.1:8010/ws'

/** WebSocket 单条子用例只读回放时的元数据快照。 */
export interface StreamReadonlyReplayMeta {
  historyId: string
  caseId: string
  round: number
  caseIndex?: number
  label: string
  status: string
  tone?: string
}

export interface StreamWorkbenchSnapshot {
  url: string
  subprotocols: string
  scheduled: string
  urlPrefix: string
  headerRows: HeaderRow[]
  outgoing: string
  filter: 'all' | 'in' | 'out'
  autoScroll: boolean
  activeHistoryId: string | null
  activeSessionId: string | null
  activeSuiteId: string | null
  suiteStatus: StreamSuiteStatus | null
  session: StreamSession | null
  events: StreamEvent[]
  cursor: number
  collapsedSequences: number[]
  subCases: StreamSubCaseSummary[]
  subCasesHistoryId: string | null
  readonlyReplay: StreamReadonlyReplayMeta | null
  draftVersion: number
}

function cloneHeaderRows(rows: HeaderRow[]): HeaderRow[] {
  if (!rows.length) return [{ name: '', value: '' }]
  return rows.map((row) => ({ name: row.name, value: row.value }))
}

/** 跨路由切换保留 WebSocket 工作台的编辑器状态、活跃运行引用、时间线事件与只读回放锁定态。 */
export const useStreamWorkbenchStore = defineStore('streamWorkbench', () => {
  const hydrated = ref(false)
  const form = reactive({
    url: DEFAULT_WS_URL,
    subprotocols: '',
    scheduled: '',
  })
  const urlPrefix = ref('')
  const headerRows = ref<HeaderRow[]>([{ name: '', value: '' }])
  const outgoing = ref('')
  const filter = ref<'all' | 'in' | 'out'>('all')
  const autoScroll = ref(true)
  const activeHistoryId = ref<string | null>(null)
  const activeSessionId = ref<string | null>(null)
  const activeSuiteId = ref<string | null>(null)
  const suiteStatus = ref<StreamSuiteStatus | null>(null)
  const session = ref<StreamSession | null>(null)
  const events = ref<StreamEvent[]>([])
  const cursor = ref(0)
  const collapsedSequences = ref<number[]>([])
  const subCases = ref<StreamSubCaseSummary[]>([])
  const subCasesHistoryId = ref<string | null>(null)
  const readonlyReplay = ref<StreamReadonlyReplayMeta | null>(null)
  const draftVersion = ref(0)

  /** 整体同步工作台状态快照，确保离开页面再返回时输入、时间线与回放锁定态均不丢失。 */
  function saveSnapshot(snapshot: StreamWorkbenchSnapshot): void {
    hydrated.value = true
    form.url = snapshot.url
    form.subprotocols = snapshot.subprotocols
    form.scheduled = snapshot.scheduled
    urlPrefix.value = snapshot.urlPrefix
    headerRows.value = cloneHeaderRows(snapshot.headerRows)
    outgoing.value = snapshot.outgoing
    filter.value = snapshot.filter
    autoScroll.value = snapshot.autoScroll
    activeHistoryId.value = snapshot.activeHistoryId
    activeSessionId.value = snapshot.activeSessionId
    activeSuiteId.value = snapshot.activeSuiteId
    suiteStatus.value = snapshot.suiteStatus
      ? {
          ...snapshot.suiteStatus,
          ...(snapshot.suiteStatus.case_links
            ? { case_links: snapshot.suiteStatus.case_links.slice() }
            : {}),
        }
      : null
    session.value = snapshot.session ? { ...snapshot.session } : null
    events.value = snapshot.events.slice()
    cursor.value = snapshot.cursor
    collapsedSequences.value = snapshot.collapsedSequences.slice()
    subCases.value = snapshot.subCases.slice()
    subCasesHistoryId.value = snapshot.subCasesHistoryId
    readonlyReplay.value = snapshot.readonlyReplay ? { ...snapshot.readonlyReplay } : null
    draftVersion.value = snapshot.draftVersion
  }

  function setActiveSession(id: string | null): void {
    hydrated.value = true
    activeSessionId.value = id
  }

  function setActiveSuite(id: string | null, status?: StreamSuiteStatus | null): void {
    hydrated.value = true
    activeSuiteId.value = id
    if (status !== undefined) {
      suiteStatus.value = status ? { ...status } : null
    }
  }

  function setReadonlyReplay(meta: StreamReadonlyReplayMeta | null): void {
    hydrated.value = true
    readonlyReplay.value = meta ? { ...meta } : null
  }

  /** 清空当前工作台数据并保持 hydrated，避免返回页面时回填默认示例 URL。 */
  function clearWorkspace(nextDraftVersion?: number): void {
    hydrated.value = true
    form.url = ''
    form.subprotocols = ''
    form.scheduled = ''
    urlPrefix.value = ''
    headerRows.value = [{ name: '', value: '' }]
    outgoing.value = ''
    filter.value = 'all'
    autoScroll.value = true
    activeHistoryId.value = null
    activeSessionId.value = null
    activeSuiteId.value = null
    suiteStatus.value = null
    session.value = null
    events.value = []
    cursor.value = 0
    collapsedSequences.value = []
    subCases.value = []
    subCasesHistoryId.value = null
    readonlyReplay.value = null
    if (typeof nextDraftVersion === 'number') {
      draftVersion.value = nextDraftVersion
    }
  }

  return {
    hydrated,
    form,
    urlPrefix,
    headerRows,
    outgoing,
    filter,
    autoScroll,
    activeHistoryId,
    activeSessionId,
    activeSuiteId,
    suiteStatus,
    session,
    events,
    cursor,
    collapsedSequences,
    subCases,
    subCasesHistoryId,
    readonlyReplay,
    draftVersion,
    saveSnapshot,
    setActiveSession,
    setActiveSuite,
    setReadonlyReplay,
    clearWorkspace,
  }
})
