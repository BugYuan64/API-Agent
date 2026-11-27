<script setup lang="ts">
/** Document-driven automatic testing, with persistent history, cross-page reconnect, and read-only AI case replay. */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import HistoryList, { type HistoryEntry } from '@/components/HistoryList.vue'
import TaskResultTree from '@/components/TaskResultTree.vue'
import TaskDraft from '@/components/TaskDraft.vue'
import TaskSummary from '@/components/TaskSummary.vue'
import TaskChat from '@/components/TaskChat.vue'
import { COPY } from '@/lib/copy'
import ResponseDetails from '@/components/ResponseDetails.vue'
import TestSettingsFields from '@/components/TestSettingsFields.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import UrlInput from '@/components/UrlInput.vue'
import { describeError } from '@/api/client'
import { cancelTask, deleteTask, followTask, generateProjectDocument, listTasks, readTask, readTaskDocument, recognizeDocument, regenerateTests, rerunTask, startTask, taskHistoryLabel, type Catalog, type TaskDocument, type TaskHistory, type TaskRecord, type TestCase } from '@/api/tasks'
import type { AssistantRunBoundPayload, CaseLinkBlock, ConsoleSseEvent, PageScope } from '@/api/types'
import { useToastStore } from '@/stores/toasts'
import { useWorkbenchRunsStore, type ReadonlyReplayState } from '@/stores/workbenchRuns'
import { useTasksWorkbenchStore } from '@/stores/tasksWorkbench'
import { caseVerdict, countCaseOutcomes, taskProgress } from '@/lib/results'
import {
  activeTaskWait,
  buildTaskResultTree,
  catalogFromHistory,
  findTaskCaseForResult,
  mergeTaskResults,
  operationDraftKey,
  parseTaskCaseId,
  taskHasTlsFailure,
  type DraftLocation,
} from '@/lib/taskResults'
import { taskSettings } from '@/lib/testSettings'
import { joinTargetUrl, urlSuffix } from '@/lib/urlPrefixes'
import { buildTaskTestContext, type TaskDraftEndpointSummary } from '@/lib/assistantContext'
import { removeTestChat } from '@/lib/chatHistory'

function resolveWorkbenchRuns() {
  if (typeof useWorkbenchRunsStore !== 'function') return null
  try {
    return useWorkbenchRunsStore()
  } catch {
    return null
  }
}

function resolveTasksWorkbench() {
  if (typeof useTasksWorkbenchStore !== 'function') return null
  try {
    return useTasksWorkbenchStore()
  } catch {
    return null
  }
}

const workbenchRuns = resolveWorkbenchRuns()
const tasksStore = resolveTasksWorkbench()

const catalog = ref<Catalog | null>(tasksStore?.catalog ?? null)
const replaySource = ref<TaskRecord | null>(tasksStore?.replaySource ?? null)
const replayEdits = ref<Record<number, TestCase>>(tasksStore ? { ...tasksStore.replayEdits } : {})
const documentInput = ref<TaskDocument | null>(tasksStore?.document ?? null)
const fileInput = ref<HTMLInputElement | null>(null)
const resultTreeRef = ref<{ focusCase: (caseId: string) => boolean } | null>(null)
const filename = ref(tasksStore?.filename ?? '')
const displayedFilename = computed(() => filename.value || replaySource.value?.snapshot.document?.filename || '')
const generating = ref(false)
const generationError = ref('')
const editing = ref(false)
const chatting = ref(false)
const creatingDocument = ref(false)
const selecting = ref(false)
const selected = ref<string[]>([])
const liveEvents = ref<ConsoleSseEvent[]>([])
const liveEventsSeen = ref(0)
const origin = ref(tasksStore?.origin ?? '')
const urlPrefix = ref(tasksStore?.urlPrefix ?? '')
const busy = ref(false)
const loadingHistory = ref(false)
const run = ref<TaskRecord | null>(tasksStore?.run ?? null)
const draftVersion = ref(tasksStore?.draftVersion ?? 0)
const resultsCleared = ref(tasksStore?.resultsCleared ?? false)
const history = ref<TaskHistory[]>([])
const readonlyReplay = ref<ReadonlyReplayState | null>(
  workbenchRuns?.readonlyReplay.tasks
    ? { ...workbenchRuns.readonlyReplay.tasks }
    : tasksStore?.readonlyReplay
      ? { ...tasksStore.readonlyReplay }
      : null,
)
const readonlyCaseStatus = ref<string | null>(null)
const toast = useToastStore()

const isReadOnlyReplay = computed(() => Boolean(readonlyReplay.value))
const running = computed(() => !!run.value && ['queued', 'running'].includes(run.value.status))
const replayDraft = computed(() => {
  const snapshot = replaySource.value?.snapshot
  return snapshot && { ...snapshot, cases: snapshot.cases.map((input, index) => replayEdits.value[index] ?? input) }
})
const savedInterfaceCount = computed(() => replayDraft.value ? buildTaskResultTree(replayDraft.value, []).length : 0)
const total = computed(() => catalog.value ? catalog.value.cases.length + catalog.value.endpoints.reduce((sum, endpoint) => sum + 1 + (endpoint.cases?.length ?? 0) + (endpoint.generated_cases?.length ?? 0), 0) : 0)
const draftLocked = computed(() => busy.value || loadingHistory.value || running.value || generating.value || creatingDocument.value || isReadOnlyReplay.value)
const hasDocument = computed(() => !!(documentInput.value || replaySource.value?.snapshot.document))
const hasOperations = computed(() => !!(catalog.value ? catalog.value.endpoints.length : savedInterfaceCount.value))
const progress = computed(() => run.value ? taskProgress(run.value.status) : null)
const outcomes = computed(() => countCaseOutcomes(run.value?.results ?? []).filter(item => item.count > 0))
const streamWait = computed(() => run.value ? activeTaskWait(run.value) : null)
const targetBase = computed(() => joinTargetUrl(urlPrefix.value, origin.value))

const plannedTotalCases = computed(() =>
  run.value ? run.value.snapshot.cases.length * Math.max(1, run.value.total_rounds ?? 1) : 0,
)
const runStageLabel = computed(() => {
  if (!run.value) return '内置契约引擎'
  if (run.value.stage === 'cooling_down') {
    return (run.value.total_rounds ?? 1) > 1
      ? `第 ${Math.max(1, run.value.current_round || 1)} / ${run.value.total_rounds} 轮 · 冷却中`
      : '冷却中'
  }
  if (run.value.stage === 'summarizing') return '正在生成总结'
  return '内置契约引擎'
})

const draftEndpoints = computed<TaskDraftEndpointSummary[]>(() => {
  if (catalog.value) {
    const endpoints = (catalog.value.endpoints ?? []).map(endpoint => ({
      method: endpoint.method ?? 'GET',
      path: endpoint.path_template || (endpoint.path ?? '').split('?')[0],
      protocol: endpoint.protocol ?? 'http',
      ...(endpoint.name ? { name: endpoint.name } : {}),
    }))
    const standalone = (catalog.value.cases ?? []).map(item => ({
      method: item.method ?? 'GET',
      path: item.path_template || (item.path ?? '').split('?')[0],
      protocol: item.protocol ?? 'http',
      ...(item.name ? { name: item.name } : {}),
    }))
    return [...endpoints, ...standalone]
  }
  if (replayDraft.value) {
    return buildTaskResultTree(replayDraft.value, []).map(group => ({
      method: group.method,
      path: group.path,
      protocol: group.protocol,
      ...(group.example?.name ? { name: group.example.name } : {}),
    }))
  }
  return []
})

const assistantSettings = computed(() => ({
  tests_per_url: taskSettings.testsPerUrl,
  testsPerUrl: taskSettings.testsPerUrl,
  allow_writes: taskSettings.allowWrites,
  observation_ms: taskSettings.observationMs,
  wall_time_seconds: taskSettings.wallTimeSeconds,
  cooldown_ms: taskSettings.cooldownMs,
}))

const assistantContext = computed(() => buildTaskTestContext({
  origin: targetBase.value || origin.value,
  run: run.value,
  resultsCleared: resultsCleared.value,
  draftEndpointCount: catalog.value ? catalog.value.endpoints.length : savedInterfaceCount.value,
  draftCaseCount: total.value || replayDraft.value?.cases.length || 0,
  settings: {
    testsPerUrl: taskSettings.testsPerUrl,
    allowWrites: taskSettings.allowWrites,
    observationMs: taskSettings.observationMs,
    wallTimeSeconds: taskSettings.wallTimeSeconds,
    cooldownMs: taskSettings.cooldownMs,
  },
  draftEndpoints: draftEndpoints.value.length ? draftEndpoints.value : undefined,
}))

const baseCatalog = computed<Catalog | null>(() =>
  catalog.value ?? (replayDraft.value ? catalogFromHistory(replayDraft.value, replaySource.value?.title) : null),
)

const focusedCaseInfo = computed(() =>
  readonlyReplay.value ? findTaskCaseForResult(run.value, readonlyReplay.value.caseId) : null,
)
const focusedCaseIndex = computed<number | null>(() => {
  if (!readonlyReplay.value) return null
  if (focusedCaseInfo.value) return focusedCaseInfo.value.caseIndex
  const parsed = parseTaskCaseId(readonlyReplay.value.caseId)
  if (parsed) return parsed.caseIndex
  const trimmed = readonlyReplay.value.caseId.trim()
  return /^\d+$/.test(trimmed) ? Number(trimmed) : null
})
const focusedCaseVerdict = computed(() => {
  if (!readonlyReplay.value) return null
  const result = focusedCaseInfo.value?.result
  if (result) return caseVerdict(result.status, result.reason)
  if (readonlyCaseStatus.value) return caseVerdict(readonlyCaseStatus.value)
  return { label: '只读回放', tone: 'info', symbol: '◎' }
})

const tlsFailure = computed(() => run.value ? taskHasTlsFailure(run.value) : false)
const entries = computed<HistoryEntry[]>(() => history.value.map(item => {
  const { label, tone } = taskProgress(item.status)
  return {
    id: item.id,
    label: taskHistoryLabel(item),
    status: label,
    tone,
    meta: new Date(item.created_at).toLocaleString(),
    at: item.created_at,
    source: item.source,
    badges: item.badge || item.source === 'ai' ? [{ label: item.badge || 'AI 测试', tone: 'info' as const }] : undefined,
    running: item.status === 'queued' || item.status === 'running',
  }
}).sort((first, second) => second.at.localeCompare(first.at)))

let generation = 0
let workspaceGeneration = 0
let timer: number | undefined
let abort: AbortController | undefined
let generationAbort: AbortController | undefined
let disposed = false

/** Sync local workbench refs into Pinia so route switches preserve draft, active run, and replay lock. */
function persistToStore() {
  if (!tasksStore) return
  tasksStore.origin = origin.value
  tasksStore.urlPrefix = urlPrefix.value
  tasksStore.catalog = catalog.value
  tasksStore.document = documentInput.value
  tasksStore.filename = filename.value
  tasksStore.run = run.value
  tasksStore.activeTaskId = run.value?.id ?? null
  tasksStore.replaySource = replaySource.value
  tasksStore.replayEdits = { ...replayEdits.value }
  tasksStore.resultsCleared = resultsCleared.value
  tasksStore.draftVersion = draftVersion.value
  tasksStore.readonlyReplay = readonlyReplay.value ? { ...readonlyReplay.value } : null
  tasksStore.focusedCaseId = readonlyReplay.value?.caseId ?? null
}

/** Keep cross-page workbenchRuns handles aligned with the current task lifecycle. */
function syncRunHandles(record: TaskRecord | null) {
  if (tasksStore) {
    tasksStore.setRun(record)
  }
  if (!workbenchRuns) return
  if (record && ['queued', 'running'].includes(record.status)) {
    workbenchRuns.setActiveRun('tasks', record.id)
    workbenchRuns.setActiveHistory('tasks', record.id)
  } else if (record) {
    if (workbenchRuns.activeRunId.tasks === record.id) {
      workbenchRuns.setActiveRun('tasks', null)
    }
    workbenchRuns.setActiveHistory('tasks', record.id)
  } else {
    workbenchRuns.setActiveHistory('tasks', null)
  }
}

function enterReadonlyReplay(payload: ReadonlyReplayState, status?: string | null) {
  readonlyReplay.value = { ...payload }
  readonlyCaseStatus.value = status ?? null
  workbenchRuns?.enterReadonlyReplay('tasks', payload)
  tasksStore?.setReadonlyReplay(payload)
}

function exitReadonlyReplay() {
  readonlyReplay.value = null
  readonlyCaseStatus.value = null
  workbenchRuns?.exitReadonlyReplay('tasks')
  tasksStore?.clearReadonlyReplay()
}

watch(
  [origin, urlPrefix, catalog, documentInput, filename, run, replaySource, replayEdits, resultsCleared, draftVersion, readonlyReplay],
  () => {
    if (!disposed) persistToStore()
  },
  { flush: 'sync' },
)

/** Invalidate in-flight observations before switching records or leaving this page. */
function stopPolling() { generation++; window.clearTimeout(timer); abort?.abort(); loadingHistory.value = false }
/** Refresh persistent history without replacing the currently selected result. */
async function refreshHistory() {
  const page = await listTasks()
  if (!disposed) {
    history.value = page.items
  }
}

/** Stream live task frames and merge multi-round results without cancelling backend runs on unmount. */
async function followActiveTask(id: string, current: number, signal: AbortSignal) {
  try {
    await followTask(id, signal, (frame, first) => {
      if (disposed || current !== generation || run.value?.id !== id) return
      const { type, events, events_seen, results, ...progressFields } = frame
      const casesPerRound = run.value.snapshot.cases.length
      const nextResults = first ? results : mergeTaskResults(run.value.results, results, casesPerRound)
      run.value = { ...run.value, ...progressFields, results: nextResults }
      liveEvents.value = events
      liveEventsSeen.value = events_seen
      syncRunHandles(run.value)
    })
  } catch {
    if (disposed || current !== generation) return
    timer = window.setTimeout(() => void openHistory(id), 800)
    return
  }
  if (!disposed && current === generation) {
    syncRunHandles(run.value)
    await refreshHistory()
  }
}

/** Load a task using generation checks so stale responses cannot overwrite a newer selection. */
async function openHistory(
  id: string,
  reveal = false,
  options?: { keepReadonlyReplay?: boolean; detachFollow?: boolean },
) {
  stopPolling()
  const current = generation
  abort = new AbortController()
  loadingHistory.value = true
  try {
    const record = await readTask(id, abort.signal)
    if (disposed || current !== generation) return
    loadingHistory.value = false
    run.value = record
    liveEvents.value = []
    liveEventsSeen.value = 0
    syncRunHandles(record)
    if (reveal) {
      if (!options?.keepReadonlyReplay) exitReadonlyReplay()
      resultsCleared.value = false
      catalog.value = null
      documentInput.value = null
      filename.value = ''
      replaySource.value = null
      replayEdits.value = {}
      generationError.value = ''
      origin.value = ''
      urlPrefix.value = ''
      editing.value = false
      stopSelection()
      if (record.snapshot.origin && record.snapshot.cases.length) {
        replaySource.value = record
        origin.value = record.snapshot.origin
      }
    }
    persistToStore()
    if (['queued', 'running'].includes(record.status)) {
      if (options?.detachFollow) {
        void followActiveTask(id, current, abort.signal)
        return
      }
      await followActiveTask(id, current, abort.signal)
      return
    }
    if (!disposed && current === generation) await refreshHistory()
  } catch (error) {
    if (current === generation && !disposed) toast.push('danger', '读取任务失败', describeError(error))
  } finally {
    if (current === generation) loadingHistory.value = false
  }
}

/** An explicit history selection can redisplay a cleared record and exit read-only replay. */
function selectHistory(id: string) {
  if (busy.value || generating.value || editing.value || creatingDocument.value || selecting.value || running.value && run.value?.stage === 'summarizing') return
  exitReadonlyReplay()
  return openHistory(id, true)
}

/** Hide results while retaining the task for polling, history updates and cancellation. */
function clearResults() {
  resultsCleared.value = true
}

/** Exit read-only replay or finished inspection, unlock editable catalog draft, and start a fresh blank chat. */
function testAgain() {
  if (creatingDocument.value || (running.value && run.value?.stage === 'summarizing')) return
  workspaceGeneration++
  stopPolling()
  generationAbort?.abort()
  generationAbort = undefined

  const sourceSnapshot = replayDraft.value ?? run.value?.snapshot
  const sourceTitle = replaySource.value?.title ?? run.value?.title
  const nextOrigin = targetBase.value || origin.value || sourceSnapshot?.origin || ''

  exitReadonlyReplay()
  if (!catalog.value && sourceSnapshot && sourceSnapshot.cases.length) {
    catalog.value = catalogFromHistory(sourceSnapshot, sourceTitle)
  }
  replaySource.value = null
  replayEdits.value = {}
  generationError.value = ''
  generating.value = false
  editing.value = false
  stopSelection()
  if (nextOrigin) {
    urlPrefix.value = ''
    origin.value = nextOrigin
  }
  busy.value = false
  run.value = null
  resultsCleared.value = false
  liveEvents.value = []
  liveEventsSeen.value = 0
  draftVersion.value++
  workbenchRuns?.setActiveRun('tasks', null)
  workbenchRuns?.setActiveHistory('tasks', null)
  if (tasksStore) {
    tasksStore.detachedFromActive = true
  }
  persistToStore()
}

/** Discard the visible draft and detach from the old run while preserving history. */
function newTest() {
  if (creatingDocument.value || run.value?.stage === 'summarizing' && running.value) return
  workspaceGeneration++
  stopPolling()
  generationAbort?.abort()
  generationAbort = undefined
  exitReadonlyReplay()
  catalog.value = null
  replaySource.value = null
  replayEdits.value = {}
  documentInput.value = null
  filename.value = ''
  generationError.value = ''
  generating.value = false
  editing.value = false
  stopSelection()
  origin.value = ''
  urlPrefix.value = ''
  busy.value = false
  run.value = null
  draftVersion.value++
  resultsCleared.value = false
  liveEvents.value = []
  liveEventsSeen.value = 0
  workbenchRuns?.setActiveRun('tasks', null)
  workbenchRuns?.setActiveHistory('tasks', null)
  tasksStore?.resetWorkspace({ detached: true })
  if (tasksStore) tasksStore.draftVersion = draftVersion.value
}

/** Handle AI assistant binding or forking a new Smart Group Testing task. */
async function handleRunBound(payload: AssistantRunBoundPayload) {
  const historyId = payload.history_id
  if (!historyId || disposed) return
  exitReadonlyReplay()
  resultsCleared.value = false
  if (tasksStore) tasksStore.detachedFromActive = false
  void refreshHistory().catch(error => toast.push('danger', '历史加载失败', describeError(error)))
  await openHistory(historyId, true, { detachFollow: true })
}

/** Reload final results and summary when the AI assistant finishes a Smart Group Testing run. */
async function handleRunCompleted(payload: { scope: PageScope; historyId: string | null }) {
  if (disposed) return
  void refreshHistory().catch(error => toast.push('danger', '历史加载失败', describeError(error)))
  if (!payload.historyId) return
  const keepReplay = Boolean(readonlyReplay.value && readonlyReplay.value.historyId === payload.historyId)
  const shouldReveal = !run.value || (run.value.id !== payload.historyId && !isReadOnlyReplay.value)
  if (shouldReveal || run.value?.id === payload.historyId) {
    await openHistory(payload.historyId, shouldReveal, { keepReadonlyReplay: keepReplay, detachFollow: true })
  }
}

/** Enter locked read-only replay mode for the clicked AI sub-case chip and focus it in both draft and result tree. */
async function handleSelectCase(block: CaseLinkBlock) {
  if (!block || !block.history_id || !block.case_id || disposed) return
  resultsCleared.value = false
  editing.value = false
  stopSelection()

  if (!run.value || run.value.id !== block.history_id) {
    await openHistory(block.history_id, true, { keepReadonlyReplay: true, detachFollow: true })
    if (disposed) return
  } else if (!replaySource.value && run.value.snapshot.cases.length) {
    catalog.value = null
    replaySource.value = run.value
    replayEdits.value = {}
    if (run.value.snapshot.origin) {
      urlPrefix.value = ''
      origin.value = run.value.snapshot.origin
    }
  }

  const resolved = findTaskCaseForResult(run.value, block.case_id)
  const targetCaseId = resolved?.caseId ?? block.case_id
  const targetRound = resolved?.round ?? block.round ?? 1
  const targetLabel = block.label || resolved?.testCase?.name || resolved?.result?.name || targetCaseId

  enterReadonlyReplay(
    {
      historyId: block.history_id,
      caseId: targetCaseId,
      round: targetRound,
      label: targetLabel,
    },
    resolved?.result?.status ?? block.status,
  )
  persistToStore()
  await nextTick()
  resultTreeRef.value?.focusCase(targetCaseId)
}

/** Make any explicit base path editable as a suffix. */
function selectUrlPrefix(prefix: string) {
  if (isReadOnlyReplay.value) return
  origin.value = prefix ? urlSuffix(origin.value, prefix, urlPrefix.value) : targetBase.value || origin.value
  urlPrefix.value = prefix
}

/** Update the form only. A fresh execution still requires an explicit submit. */
function useHttpTarget() {
  if (isReadOnlyReplay.value) return
  const resolved = run.value?.snapshot.origin
  if (!tlsFailure.value || !resolved) return
  urlPrefix.value = ''
  origin.value = resolved.replace(/^https:/, 'http:')
}

/** Import one bounded UTF-8 file, then generate a reviewable draft without target calls. */
async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || draftLocked.value || editing.value || selecting.value) return
  catalog.value = null
  replaySource.value = null
  replayEdits.value = {}
  filename.value = file.name
  documentInput.value = null
  generationError.value = ''
  generating.value = true
  const workspace = workspaceGeneration
  try {
    if (file.size > 524288) throw new Error('文档最大 512 KiB')
    const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer())
    if (!disposed && workspace === workspaceGeneration) documentInput.value = { filename: file.name, text }
  } catch (error) {
    if (!disposed && workspace === workspaceGeneration) generationError.value = describeError(error)
  } finally {
    if (workspace === workspaceGeneration) generating.value = false
  }
  if (!disposed && workspace === workspaceGeneration && documentInput.value) await prepareDraft('import')
}

/** Read a history attachment only when needed for a new draft operation. */
async function sourceDocument(signal?: AbortSignal) {
  if (documentInput.value) return documentInput.value
  const source = replaySource.value
  const attachment = source?.snapshot.document
  return source && attachment ? { filename: attachment.filename, text: await readTaskDocument(source.id, signal) } : undefined
}

/** Prepare changes atomically; generation failures never erase an existing draft. */
async function prepareDraft(mode: 'import' | 'reparse' | 'selected' = 'import') {
  if (disposed || draftLocked.value || editing.value) return
  const previous = catalog.value ?? (replayDraft.value ? catalogFromHistory(replayDraft.value, replaySource.value?.title) : undefined)
  const indices = previous?.endpoints.flatMap((endpoint, index) => selected.value.includes(operationDraftKey(endpoint)) ? [index] : []) ?? []
  if (mode === 'selected' && (!previous || !indices.length)) return
  generating.value = true
  generationError.value = ''
  const controller = new AbortController()
  generationAbort = controller
  try {
    const document = await sourceDocument(controller.signal)
    if (mode !== 'selected' && !document) return
    const recognized = mode === 'selected'
      ? await regenerateTests(previous!, indices, taskSettings.testsPerUrl, document, controller.signal)
      : await recognizeDocument(document!, taskSettings.testsPerUrl, controller.signal, mode === 'reparse' && (previous?.endpoints.length || previous?.cases.length) ? previous : undefined)
    if (!disposed && generationAbort === controller) {
      catalog.value = recognized
      if (document) {
        documentInput.value = document
        filename.value = document.filename
      }
      stopSelection()
      if (mode === 'import' && recognized.server_url) {
        urlPrefix.value = ''
        origin.value = recognized.server_url
      }
    }
  } catch (error) {
    if (!disposed && generationAbort === controller) generationError.value = describeError(error)
  } finally {
    if (generationAbort === controller) {
      generationAbort = undefined
      generating.value = false
    }
  }
}

/** Keep status and file selection in this workspace while the backend owns the native dialog. */
async function generateDocument() {
  if (draftLocked.value || editing.value || selecting.value) return
  creatingDocument.value = true
  generationError.value = ''
  let imported = false
  try {
    const document = await generateProjectDocument()
    if (!document || disposed) return
    toast.push(document.partial ? 'warning' : 'success', `已保存 API/${document.filename}`,
      document.partial ? '文档包含待核对项' : undefined)
    if (document.text == null) {
      generationError.value = '文档已保存，但超过自动导入的 512 KiB 上限。'
      return
    }
    newDocumentDraft({ filename: document.filename, text: document.text })
    imported = true
  } catch (error) {
    if (!disposed) generationError.value = describeError(error)
  } finally {
    creatingDocument.value = false
  }
  if (imported) await prepareDraft('import')
}

/** Set up the generated attachment as a new import, independent of selected history. */
function newDocumentDraft(document: TaskDocument) {
  catalog.value = null
  replaySource.value = null
  replayEdits.value = {}
  documentInput.value = document
  filename.value = document.filename
}

function stopSelection() { selecting.value = false; selected.value = [] }

/** Identify selection by operation rather than its mutable position. */
function toggleOperation(key: string) {
  if (draftLocked.value) return
  selected.value = selected.value.includes(key) ? selected.value.filter(item => item !== key) : [...selected.value, key]
}

/** Switch into selection without replacing the history draft or sending a model request. */
function beginSelection() {
  if (draftLocked.value || editing.value || !hasOperations.value) return
  selecting.value = true
  selected.value = []
}

/** Drop only the chosen operation from the editable draft, never from its history. */
function removeOperation(index: number) {
  if (draftLocked.value || editing.value || selecting.value) return
  const current = catalog.value ?? (replayDraft.value ? catalogFromHistory(replayDraft.value, replaySource.value?.title) : undefined)
  const endpoint = current?.endpoints[index]
  if (!current || !endpoint) return
  const route = operationDraftKey(endpoint)
  const concrete = operationDraftKey({ ...endpoint, path_template: null })
  catalog.value = { ...current, endpoints: current.endpoints.filter((_, position) => position !== index),
    cases: current.cases.filter(item => ![route, concrete].includes(operationDraftKey(item))) }
}

/** Store edits in the active draft, leaving recorded inputs and evidence untouched. */
function changeDraft(location: DraftLocation, input: TestCase) {
  if (isReadOnlyReplay.value) return
  if (location.kind === 'history') {
    if (replaySource.value?.snapshot.cases[location.index]) {
      replayEdits.value[location.index] = input
      persistToStore()
    }
    return
  }
  if (!catalog.value) return
  if (location.kind === 'standalone') {
    catalog.value.cases[location.index] = input
    persistToStore()
    return
  }
  const endpoint = catalog.value.endpoints[location.operation]
  if (!endpoint) return
  if (location.kind === 'example') {
    catalog.value.endpoints[location.operation] = { ...endpoint, ...input }
  } else {
    const cases = endpoint[location.kind]
    if (cases?.[location.index]) cases[location.index] = input
  }
  persistToStore()
}

/** Submit the recognized catalog with the current global limits and permission, then follow progress. */
async function submit() {
  const imported = catalog.value
  const source = replaySource.value
  if ((!imported && !source) || (imported && !total.value) || !targetBase.value || draftLocked.value || editing.value || selecting.value) return
  stopPolling()
  busy.value = true
  const workspace = workspaceGeneration
  try {
    const options = {
      origin: targetBase.value,
      allow_writes: taskSettings.allowWrites,
      observation_ms: taskSettings.observationMs,
      wall_time_seconds: taskSettings.wallTimeSeconds,
      cooldown_ms: taskSettings.cooldownMs,
    }
    const document = imported ? await sourceDocument() : undefined
    if (disposed || workspace !== workspaceGeneration) return
    const record = imported
      ? await startTask({ ...options, catalog: imported, ...(document ? { document } : {}) })
      : await rerunTask(source!.id, {
        ...options,
        ...(Object.keys(replayEdits.value).length ? { case_edits: replayEdits.value } : {}),
      })
    if (disposed) return
    if (workspace === workspaceGeneration) {
      exitReadonlyReplay()
      run.value = record
      syncRunHandles(record)
      if (source) {
        replaySource.value = record
        replayEdits.value = {}
        if (imported) {
          catalog.value = null
          documentInput.value = null
          filename.value = ''
        }
      }
      resultsCleared.value = false
      persistToStore()
      void openHistory(record.id)
    }
    void refreshHistory().catch(error => toast.push('danger', '历史加载失败', describeError(error)))
  } catch (error) {
    if (!disposed && workspace === workspaceGeneration) toast.push('danger', '启动失败', describeError(error))
  } finally {
    if (workspace === workspaceGeneration) busy.value = false
  }
}

/** Stop polling before cancellation, preventing a late running snapshot from reverting the UI. */
async function cancel() {
  if (!run.value || run.value.stage === 'summarizing') return
  const id = run.value.id
  const workspace = workspaceGeneration
  stopPolling()
  busy.value = true
  try {
    await cancelTask(id)
    if (!disposed && workspace === workspaceGeneration) await openHistory(id)
    else if (!disposed) await refreshHistory()
  } catch (error) {
    if (!disposed && workspace === workspaceGeneration) {
      toast.push('danger', '取消失败', describeError(error))
      void openHistory(id)
    }
  } finally {
    if (workspace === workspaceGeneration) busy.value = false
  }
}

/** Remove a completed history entry through the backend lifecycle guard. */
async function removeHistory(id: string) {
  if (selecting.value || generating.value || busy.value || loadingHistory.value || editing.value || creatingDocument.value) return
  busy.value = true
  const workspace = workspaceGeneration
  try {
    await deleteTask(id)
    removeTestChat('tasks', id)
    if (workspace === workspaceGeneration && readonlyReplay.value?.historyId === id) {
      exitReadonlyReplay()
    }
    if (workspace === workspaceGeneration && replaySource.value?.id === id) {
      replaySource.value = null
      replayEdits.value = {}
      editing.value = false
    }
    if (workspace === workspaceGeneration && run.value?.id === id) {
      stopPolling()
      run.value = null
      if (workbenchRuns?.activeRunId.tasks === id) workbenchRuns.setActiveRun('tasks', null)
      workbenchRuns?.setActiveHistory('tasks', null)
    }
    persistToStore()
    await refreshHistory()
  } catch (error) { toast.push('danger', '删除失败', describeError(error)) }
  finally { if (workspace === workspaceGeneration) busy.value = false }
}

/** Restore cross-page workbench state and auto-reconnect to any active background task without losing progress. */
async function restoreOnMount() {
  const trackedActiveRunId = workbenchRuns?.activeRunId.tasks ?? null
  const trackedHistoryId = tasksStore?.activeTaskId ?? workbenchRuns?.activeHistoryId.tasks ?? run.value?.id ?? null
  const trackedReplay = workbenchRuns?.readonlyReplay.tasks ?? tasksStore?.readonlyReplay ?? null
  if (trackedReplay && !readonlyReplay.value) {
    readonlyReplay.value = { ...trackedReplay }
  }
  try {
    await refreshHistory()
  } catch (error) {
    if (!disposed) toast.push('danger', '历史加载失败', describeError(error))
  }
  if (disposed) return

  const activeInHistory = history.value.find(item => ['queued', 'running'].includes(item.status))
  const trackedIsRunning = Boolean(
    trackedHistoryId &&
    history.value.some(item => item.id === trackedHistoryId && ['queued', 'running'].includes(item.status)),
  )
  const reconnectId =
    trackedActiveRunId ||
    (trackedIsRunning ? trackedHistoryId : null) ||
    (run.value && ['queued', 'running'].includes(run.value.status) ? run.value.id : null) ||
    (!tasksStore?.detachedFromActive && !run.value && !catalog.value && activeInHistory ? activeInHistory.id : null)

  if (reconnectId) {
    const shouldReveal = !run.value && !catalog.value
    const keepReplay = Boolean(readonlyReplay.value && readonlyReplay.value.historyId === reconnectId)
    await openHistory(reconnectId, shouldReveal, { keepReadonlyReplay: keepReplay, detachFollow: true })
    if (!disposed && readonlyReplay.value) {
      await nextTick()
      resultTreeRef.value?.focusCase(readonlyReplay.value.caseId)
    }
    return
  }

  if (readonlyReplay.value) {
    if (!run.value || run.value.id !== readonlyReplay.value.historyId) {
      await openHistory(readonlyReplay.value.historyId, true, { keepReadonlyReplay: true, detachFollow: true })
    }
    if (!disposed) {
      await nextTick()
      resultTreeRef.value?.focusCase(readonlyReplay.value.caseId)
    }
  }
}

onMounted(() => { void restoreOnMount() })
onBeforeUnmount(() => {
  persistToStore()
  disposed = true
  stopPolling()
  generationAbort?.abort()
})
</script>

<template>
  <TestWorkspace history-key="api-agent.layout.cases.history" editor-key="api-agent.layout.tasks.input"
    assistant-key="api-agent.layout.tasks.summary" :editor-width="440" hide-history-on-narrow>
    <template #history><HistoryList title="测试历史" empty-text="还没有测试记录" :items="entries" :active-id="resultsCleared ? undefined : run?.id" :on-delete="removeHistory" :on-new-test="newTest" @select="selectHistory" /></template>
      <template #editor>
        <section class="task-panel scroll-y">
          <h2><span class="panel-title"><AppIcon name="workflow" /> {{ COPY.navigation.intelligentTests }}</span></h2>
          <div v-if="isReadOnlyReplay && readonlyReplay" class="readonly-banner" role="status" aria-label="只读回放提示">
            <div class="readonly-banner-head">
              <span class="tag tag-info">只读回放</span>
              <span v-if="(run?.total_rounds ?? 1) > 1 || (readonlyReplay.round ?? 1) > 1" class="tag tag-neutral">第 {{ readonlyReplay.round ?? 1 }} 轮</span>
              <span v-if="focusedCaseVerdict" class="tag verdict" :class="`tag-${focusedCaseVerdict.tone}`">{{ focusedCaseVerdict.label }}</span>
              <strong class="readonly-case-label">{{ readonlyReplay.label || readonlyReplay.caseId }}</strong>
            </div>
            <div class="readonly-banner-actions">
              <span class="hint">当前用例与表单已锁定，点击「再次测试」将解锁草稿并开启全新对话。</span>
              <button type="button" class="btn btn-primary btn-sm readonly-retest-btn" @click="testAgain">再次测试</button>
            </div>
          </div>
          <div class="upload">
            <a v-if="replaySource?.snapshot.document" class="document-name" :href="'/api/tasks/' + replaySource.id + '/document'" :title="displayedFilename" download>{{ displayedFilename }}</a>
            <span v-else class="document-name" :title="displayedFilename || '支持 JSON、YAML、Markdown 和文本，最多一个文档，最大 512 KiB'">{{ displayedFilename || '未上传接口文档' }}</span>
            <input ref="fileInput" type="file" accept=".json,.yaml,.yml,.md,.txt" hidden @change="upload" />
            <button class="btn btn-sm" type="button" :disabled="draftLocked || editing || selecting" @click="fileInput?.click()">上传文档</button>
            <button class="btn btn-sm" type="button" :disabled="draftLocked || editing || selecting"
              title="选择本机项目，使用系统配置的模型分析脱敏源码" @click="generateDocument">生成接口文档</button>
          </div>
          <p v-if="busy">正在处理，请稍候…</p>
          <p v-if="creatingDocument" class="hint" role="status">等待选择项目或生成接口文档…</p>
          <p v-if="generating" class="hint" role="status">正在处理接口并生成 test…</p>
          <p v-if="generationError" class="warning" role="alert">{{ generationError }}</p>
          <div v-if="hasDocument || hasOperations" class="draft-actions">
            <template v-if="selecting">
              <button class="btn btn-primary btn-sm" type="button" :disabled="draftLocked || !selected.length" @click="prepareDraft('selected')">生成已选 test（{{ selected.length }}）</button>
              <button class="btn btn-sm" type="button" :disabled="generating" @click="stopSelection">取消</button>
            </template>
            <template v-else>
              <button v-if="hasDocument" class="btn btn-sm" type="button" :disabled="draftLocked || editing" @click="prepareDraft(catalog || replaySource ? 'reparse' : 'import')">重新解析</button>
              <button v-if="hasOperations" class="btn btn-sm" type="button" :disabled="draftLocked || editing" @click="beginSelection">重新生成 test</button>
            </template>
          </div>
          <template v-if="catalog">
            <p>{{ catalog.endpoints.length }} 个接口 · {{ total }} 条用例（含文档原有用例）</p>
            <TaskDraft
              v-model:editing="editing"
              :catalog="catalog"
              :disabled="draftLocked"
              :readonly="isReadOnlyReplay"
              :focused-case-id="readonlyReplay?.caseId ?? null"
              :focused-case-index="focusedCaseIndex"
              :selecting="selecting"
              :selected="selected"
              @change="changeDraft"
              @remove="removeOperation"
              @toggle="toggleOperation"
            />
            <p v-for="warning in catalog.warnings" :key="warning" class="warning">{{ warning }}</p>
          </template>
          <template v-else-if="replaySource && replayDraft">
            <strong>{{ taskHistoryLabel(replaySource) }}</strong>
            <p>{{ savedInterfaceCount }} 个接口 · {{ replayDraft.cases.length }} 条已保存用例</p>
            <TaskDraft
              :key="replaySource.id"
              v-model:editing="editing"
              :snapshot="replayDraft"
              :disabled="draftLocked"
              :readonly="isReadOnlyReplay"
              :focused-case-id="readonlyReplay?.caseId ?? null"
              :focused-case-index="focusedCaseIndex"
              :selecting="selecting"
              :selected="selected"
              @change="changeDraft"
              @remove="removeOperation"
              @toggle="toggleOperation"
            />
          </template>
          <div class="field">
            <span>测试目标 URL</span>
            <UrlInput v-model:address="origin" :model-value="urlPrefix" :disabled="running || isReadOnlyReplay"
              label="测试目标 URL" placeholder="http://127.0.0.1:9100" @update:model-value="selectUrlPrefix" @submit="submit" />
          </div>
          <p class="hint">文档中的测试基址会自动填入，请确认地址后开始测试。</p>
          <details class="settings"><summary>设置</summary>
            <TestSettingsFields group="task" :disabled="running || isReadOnlyReplay" :generation-locked="draftLocked || editing" />
          </details>
          <div class="actions"><button class="btn btn-primary" :disabled="(catalog ? !total : !replaySource) || !targetBase || draftLocked || editing || selecting" @click="submit"><AppIcon name="play" />{{ replaySource ? '再次测试' : '开始测试' }}</button><button v-if="running" class="btn" :disabled="busy || run?.stage === 'summarizing'" @click="cancel">取消任务</button></div>
        </section>
      </template>
      <template #results>
        <section class="task-panel scroll-y">
          <h2>执行结果 <span class="result-actions">
            <button v-if="run && !running && !resultsCleared" type="button" class="btn btn-sm retest-btn" @click="testAgain">再次测试</button>
            <a v-if="run && !resultsCleared" class="btn btn-sm" :href="'/api/tasks/' + run.id + '/report'" download><AppIcon name="download" />导出总结</a>
            <ResponseDetails :key="run?.id" v-if="run && !resultsCleared" :data="run" note="这里展示当前任务和已记录的用例响应。响应正文或事件可能按记录上限截断。" />
            <button type="button" class="btn btn-ghost btn-sm" title="清除当前执行结果显示" :disabled="!run || resultsCleared" @click="clearResults">清除</button>
          </span></h2>
          <p v-if="!run || resultsCleared" class="hint">{{ resultsCleared && run ? COPY.feedback.clearedCurrentResult : COPY.empty.taskResults }}</p>
          <template v-else>
            <div class="run-overview" role="status">
              <div class="run-meta">
                <span v-if="progress" class="tag status-dot" :class="`tag-${progress.tone}`">{{ progress.label }}</span>
                <span v-if="run.badge || run.source === 'ai'" class="tag tag-info">{{ run.badge || 'AI 测试' }}</span>
                <span v-if="(run.total_rounds ?? 1) > 1" class="tag tag-neutral">第 {{ Math.max(1, run.current_round || 1) }} / {{ run.total_rounds }} 轮</span>
                <span v-if="run.stage === 'cooling_down'" class="tag tag-warning cooling-badge">冷却中<template v-if="run.cooldown_remaining_ms != null"> · {{ Math.ceil(run.cooldown_remaining_ms / 1000) }}s</template></span>
                <span><strong>{{ run.results.length }} / {{ plannedTotalCases }}</strong> 条已记录</span>
                <span>{{ runStageLabel }}</span>
              </div>
              <div v-if="streamWait || outcomes.length" class="outcomes" aria-label="用例结果统计">
                <span v-if="streamWait" class="tag tag-info verdict">{{ streamWait.label }} <strong>1</strong></span>
                <span v-for="outcome in outcomes" :key="outcome.status" class="tag verdict" :class="`tag-${outcome.tone}`">
                  <span aria-hidden="true">{{ outcome.symbol }}</span>{{ outcome.label }} <strong>{{ outcome.count }}</strong>
                </span>
              </div>
            </div>
            <p v-if="tlsFailure" class="warning">目标 HTTPS 连接失败。若服务使用 HTTP，可将地址填回表单后重新测试。
              <button type="button" class="btn btn-sm" :disabled="running || isReadOnlyReplay" @click="useHttpTarget">填入 HTTP 地址</button>
            </p>
            <p v-if="run.error_kind" class="warning">{{ run.error_kind }}；已记录结果仍可导出。</p>
            <TaskResultTree
              ref="resultTreeRef"
              :key="run.id"
              :task="run"
              :stream-wait="streamWait"
              :live-events="liveEvents"
              :live-events-seen="liveEventsSeen"
              :focused-case-id="readonlyReplay?.caseId ?? null"
            />
            <TaskSummary :text="run.summary" :pending="running && run.stage === 'summarizing'"
              :status="run.stage === 'summarizing' ? '正在生成总结…' : run.stage === 'cooling_down' ? '轮间冷却中，等待下一轮执行…' : running ? '等待测试执行完成。' : '暂无 AI 总结，请查看逐条结果。'" />
          </template>
        </section>
      </template>
      <template #assistant>
      <TaskChat
        :scope="'tasks'"
        :task-id="resultsCleared ? undefined : run?.id"
        :execution-id="run?.id ?? null"
        :draft-version="draftVersion"
        :current-test="assistantContext"
        :settings="assistantSettings"
        :base-catalog="baseCatalog"
        :active-case-id="readonlyReplay?.caseId ?? null"
        @busy="chatting = $event"
        @run-bound="handleRunBound"
        @run-completed="handleRunCompleted"
        @select-case="handleSelectCase"
      />
      </template>
  </TestWorkspace>
</template>
<style scoped>
.task-panel { height: 100%; min-height: 0; padding: 18px; box-sizing: border-box; }
h2 { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 8px; font-size: 15px; margin: 0 0 16px; }
.result-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.field { display: flex; flex-direction: column; gap: 8px; margin: 16px 0; }
.panel-title { display: inline-flex; align-items: center; gap: 8px; }
.panel-title svg { color: var(--accent); }
.readonly-banner {
  display: grid;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--info-border);
  border-radius: var(--radius-md);
  background: var(--info-soft);
}
.readonly-banner-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.readonly-case-label {
  font-size: 12.5px;
  overflow-wrap: anywhere;
}
.readonly-banner-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.readonly-banner-actions .hint {
  margin: 0;
  font-size: 12px;
}
.upload { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 6px 8px; border: 1px solid var(--accent-border); border-radius: var(--radius-md); background: var(--bg-subtle); }
.upload:focus-within { border-color: var(--accent); }
.document-name { flex: 1 1 120px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.upload .btn { flex: none; }
.draft-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.settings {
  margin-top: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  padding: 7px 10px;
  transition: border-color var(--motion-fast), background var(--motion-fast);
}
.settings:hover { border-color: var(--border-default); }
.settings[open] { background: var(--bg-surface); border-color: var(--border-default); }
.settings > summary {
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  user-select: none;
}
.settings[open] > summary { color: var(--text-primary); }
.settings > :not(summary) { margin-top: 8px; }
.hint { line-height: 1.7; color: var(--text-tertiary); }
.actions { display: flex; gap: 8px; margin-top: 20px; }
.run-overview { display: grid; gap: 10px; margin-bottom: 12px; padding: 12px; background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }
.run-meta, .outcomes { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; color: var(--text-secondary); font-size: 12px; }
.run-meta strong { color: var(--text-primary); }
.warning { color: var(--warning); }
</style>
