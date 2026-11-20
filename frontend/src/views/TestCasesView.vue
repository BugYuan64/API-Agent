<script setup lang="ts">
/** Document-driven automatic testing, with persistent history and cancellable progress polling. */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
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
import UrlPrefixPicker from '@/components/UrlPrefixPicker.vue'
import { describeError } from '@/api/client'
import { cancelTask, deleteTask, followTask, generateProjectDocument, listTasks, readTask, readTaskDocument, recognizeDocument, regenerateTests, rerunTask, startTask, taskHistoryLabel, type Catalog, type TaskDocument, type TaskHistory, type TaskRecord, type TestCase } from '@/api/tasks'
import type { ConsoleSseEvent } from '@/api/types'
import { useToastStore } from '@/stores/toasts'
import { countCaseOutcomes, taskProgress } from '@/lib/results'
import { activeTaskWait, buildTaskResultTree, catalogFromHistory, operationDraftKey, taskHasTlsFailure, type DraftLocation } from '@/lib/taskResults'
import { taskSettings } from '@/lib/testSettings'
import { joinTargetUrl, urlSuffix } from '@/lib/urlPrefixes'
import { attachChatToExecution, hasChatTranscript, isChatOnly, listChatHistories, removeChatHistory, type ChatHistoryRecord } from '@/lib/chatHistory'

const catalog = ref<Catalog | null>(null)
const replaySource = ref<TaskRecord | null>(null)
const replayEdits = ref<Record<number, TestCase>>({})
const documentInput = ref<TaskDocument | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const filename = ref('')
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
const origin = ref('')
const urlPrefix = ref('')
const busy = ref(false)
const loadingHistory = ref(false)
const run = ref<TaskRecord | null>(null)
const chatDraftKey = ref(`draft-${Date.now()}`)
const activeChatHistoryId = ref<string | null>(null)
const chatOnlyHistory = ref<ChatHistoryRecord[]>(listChatHistories('tasks'))
const resultsCleared = ref(false)
const history = ref<TaskHistory[]>([])
const toast = useToastStore()
const running = computed(() => !!run.value && ['queued', 'running'].includes(run.value.status))
const replayDraft = computed(() => {
  const snapshot = replaySource.value?.snapshot
  return snapshot && { ...snapshot, cases: snapshot.cases.map((input, index) => replayEdits.value[index] ?? input) }
})
const savedInterfaceCount = computed(() => replayDraft.value ? buildTaskResultTree(replayDraft.value, []).length : 0)
const total = computed(() => catalog.value ? catalog.value.cases.length + catalog.value.endpoints.reduce((sum, endpoint) => sum + 1 + (endpoint.cases?.length ?? 0) + (endpoint.generated_cases?.length ?? 0), 0) : 0)
const draftLocked = computed(() => busy.value || loadingHistory.value || running.value || generating.value || creatingDocument.value)
const hasDocument = computed(() => !!(documentInput.value || replaySource.value?.snapshot.document))
const hasOperations = computed(() => !!(catalog.value ? catalog.value.endpoints.length : savedInterfaceCount.value))
const progress = computed(() => run.value ? taskProgress(run.value.status) : null)
const outcomes = computed(() => countCaseOutcomes(run.value?.results ?? []).filter(item => item.count > 0))
const streamWait = computed(() => run.value ? activeTaskWait(run.value) : null)
const targetBase = computed(() => joinTargetUrl(urlPrefix.value, origin.value))
const tlsFailure = computed(() => run.value ? taskHasTlsFailure(run.value) : false)
const entries = computed<HistoryEntry[]>(() => [...history.value.map(item => {
  const { label, tone } = taskProgress(item.status)
  return { id: item.id, label: taskHistoryLabel(item), status: label, tone, meta: new Date(item.created_at).toLocaleString(), at: item.created_at }
}), ...chatOnlyHistory.value.filter(chat => chat.id.startsWith('chat-')).map(chat => ({
  id: chat.id, label: chat.title, status: '仅 AI 对话', tone: 'info', meta: new Date(chat.created_at).toLocaleString(), raw: chat, at: chat.created_at,
}))].sort((first, second) => (second.at ?? '').localeCompare(first.at ?? '')))
let generation = 0
let workspaceGeneration = 0
let timer: number | undefined
let abort: AbortController | undefined
let generationAbort: AbortController | undefined
let disposed = false

/** Invalidate in-flight observations before switching records or leaving this page. */
function stopPolling() { generation++; window.clearTimeout(timer); abort?.abort(); loadingHistory.value = false }
/** Refresh persistent history without replacing the currently selected result. */
async function refreshHistory() {
  const page = await listTasks()
  if (!disposed) {
    history.value = page.items
    chatOnlyHistory.value = listChatHistories('tasks')
  }
}
/** Load a task using generation checks so stale responses cannot overwrite a newer selection. */
async function openHistory(id: string, reveal = false) {
  stopPolling()
  const current = generation
  abort = new AbortController()
  loadingHistory.value = true
  try {
    const record = await readTask(id, abort.signal)
    if (disposed || current !== generation) return
    loadingHistory.value = false
    run.value = record
    activeChatHistoryId.value = hasChatTranscript('tasks', id) ? id : null
    chatDraftKey.value = record.id
    liveEvents.value = []
    liveEventsSeen.value = 0
    if (reveal) {
      resultsCleared.value = false
      catalog.value = null
      documentInput.value = null
      filename.value = ''
      replaySource.value = null
      replayEdits.value = {}
      generationError.value = ''
      origin.value = ''
      urlPrefix.value = ''
      stopSelection()
      if (record.snapshot.origin && record.snapshot.cases.length) {
        replaySource.value = record
        origin.value = record.snapshot.origin
      }
    }
    if (['queued', 'running'].includes(record.status)) {
      try {
        await followTask(id, abort.signal, (frame, first) => {
          if (disposed || current !== generation || run.value?.id !== id) return
          const { type, events, events_seen, results, ...progressFields } = frame
          run.value = { ...run.value, ...progressFields, results: first ? results : [...run.value.results, ...results] }
          liveEvents.value = events
          liveEventsSeen.value = events_seen
        })
      } catch {
        if (disposed || current !== generation) return
        timer = window.setTimeout(() => void openHistory(id), 800)
        return
      }
    }
    if (!disposed && current === generation) await refreshHistory()
  } catch (error) {
    if (current === generation && !disposed) toast.push('danger', '读取任务失败', describeError(error))
  } finally {
    if (current === generation) loadingHistory.value = false
  }
}
/** An explicit history selection can redisplay a cleared record. */
function selectHistory(id: string) {
  if (busy.value || generating.value || editing.value || creatingDocument.value || selecting.value || running.value && run.value?.stage === 'summarizing') return
  if (isChatOnly(id) && chatOnlyHistory.value.some(item => item.id === id)) return selectChatHistory(id)
  return openHistory(id, true)
}
/** Hide results while retaining the task for polling, history updates and cancellation. */
function clearResults() {
  resultsCleared.value = true
}

/** Discard the visible draft and detach from the old run while preserving history. */
function newTest() {
  if (creatingDocument.value || run.value?.stage === 'summarizing' && running.value) return
  workspaceGeneration++
  stopPolling()
  generationAbort?.abort()
  generationAbort = undefined
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
  activeChatHistoryId.value = null
  chatOnlyHistory.value = listChatHistories('tasks')
  chatDraftKey.value = `draft-${Date.now()}`
  resultsCleared.value = false
  liveEvents.value = []
  liveEventsSeen.value = 0
}

/** Make any explicit base path editable as a suffix. */
function selectUrlPrefix(prefix: string) {
  origin.value = prefix ? urlSuffix(origin.value, prefix, urlPrefix.value) : targetBase.value || origin.value
  urlPrefix.value = prefix
}

/** Update the form only. A fresh execution still requires an explicit submit. */
function useHttpTarget() {
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
  if (location.kind === 'history') {
    if (replaySource.value?.snapshot.cases[location.index]) replayEdits.value[location.index] = input
    return
  }
  if (!catalog.value) return
  if (location.kind === 'standalone') {
    catalog.value.cases[location.index] = input
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
}
/** Submit the recognized catalog with the current global limits and permission, then follow progress. */
async function submit() {
  const imported = catalog.value
  const source = replaySource.value
  if ((!imported && !source) || (imported && !total.value) || !targetBase.value || draftLocked.value || editing.value || selecting.value) return
  stopPolling()
  const sourceChatId = activeChatHistoryId.value && isChatOnly(activeChatHistoryId.value)
    ? activeChatHistoryId.value
    : null
  busy.value = true
  const workspace = workspaceGeneration
  try {
    const options = {
      origin: targetBase.value,
      allow_writes: taskSettings.allowWrites,
      observation_ms: taskSettings.observationMs,
      wall_time_seconds: taskSettings.wallTimeSeconds,
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
      run.value = record
      activeChatHistoryId.value = null
      if (sourceChatId) attachChatToExecution('tasks', sourceChatId, record.id)
      chatOnlyHistory.value = listChatHistories('tasks')
      chatDraftKey.value = record.id
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
  if (isChatOnly(id) && chatOnlyHistory.value.some(item => item.id === id)) {
    deleteChatHistory(id)
    return
  }
  if (selecting.value || generating.value || busy.value || loadingHistory.value || editing.value || creatingDocument.value) return
  busy.value = true
  const workspace = workspaceGeneration
  try {
    await deleteTask(id)
    removeChatHistory('tasks', id)
    chatOnlyHistory.value = chatOnlyHistory.value.filter(item => item.id !== id)
    if (workspace === workspaceGeneration && replaySource.value?.id === id) {
      replaySource.value = null
      replayEdits.value = {}
      editing.value = false
    }
    if (workspace === workspaceGeneration && run.value?.id === id) { stopPolling(); run.value = null }
    await refreshHistory()
  } catch (error) { toast.push('danger', '删除失败', describeError(error)) }
  finally { if (workspace === workspaceGeneration) busy.value = false }
}

function createChatHistory(record: ChatHistoryRecord, draftKey: string) {
  chatOnlyHistory.value = [record, ...chatOnlyHistory.value.filter(item => item.id !== record.id)]
  if (chatDraftKey.value === draftKey && activeChatHistoryId.value === null && !run.value) activeChatHistoryId.value = record.id
}

function titleChatHistoryRecord(record: ChatHistoryRecord) {
  chatOnlyHistory.value = [record, ...chatOnlyHistory.value.filter(item => item.id !== record.id)]
  if (run.value?.id === record.id) activeChatHistoryId.value = null
}

function selectChatHistory(id: string) {
  const chat = chatOnlyHistory.value.find(item => item.id === id && isChatOnly(item.id))
  if (!chat) return
  stopPolling()
  generationAbort?.abort()
  activeChatHistoryId.value = id
  chatDraftKey.value = id
  run.value = null
  liveEvents.value = []
  liveEventsSeen.value = 0
  catalog.value = null
  replaySource.value = null
  replayEdits.value = {}
  documentInput.value = null
  filename.value = ''
  generationError.value = ''
  origin.value = ''
  urlPrefix.value = ''
  resultsCleared.value = false
}

function deleteChatHistory(id: string) {
  removeChatHistory('tasks', id)
  chatOnlyHistory.value = chatOnlyHistory.value.filter(item => item.id !== id)
  if (activeChatHistoryId.value === id) newTest()
}
onMounted(() => { void refreshHistory().catch(error => toast.push('danger', '历史加载失败', describeError(error))) })
onBeforeUnmount(() => { disposed = true; stopPolling(); generationAbort?.abort() })
onBeforeRouteLeave(() => !chatting.value && !(running.value && run.value?.stage === 'summarizing'))
</script>

<template>
  <TestWorkspace history-key="api-agent.layout.cases.history" editor-key="api-agent.layout.tasks.input"
    assistant-key="api-agent.layout.tasks.summary" :editor-width="440" :upper-height="520">
    <template #history><HistoryList title="测试历史" empty-text="还没有测试或对话记录" :items="entries" :active-id="resultsCleared ? undefined : activeChatHistoryId ?? run?.id" :on-delete="removeHistory" :on-new-test="newTest" @select="selectHistory" /></template>
      <template #editor>
        <section class="task-panel scroll-y">
          <h2><span class="panel-title"><AppIcon name="workflow" /> {{ COPY.navigation.intelligentTests }}</span></h2>
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
            <TaskDraft v-model:editing="editing" :catalog="catalog" :disabled="draftLocked" :selecting="selecting" :selected="selected" @change="changeDraft" @remove="removeOperation" @toggle="toggleOperation" />
            <p v-for="warning in catalog.warnings" :key="warning" class="warning">{{ warning }}</p>
          </template>
          <template v-else-if="replaySource && replayDraft">
            <strong>{{ taskHistoryLabel(replaySource) }}</strong>
            <p>{{ savedInterfaceCount }} 个接口 · {{ replayDraft.cases.length }} 条已保存用例</p>
            <TaskDraft :key="replaySource.id" v-model:editing="editing" :snapshot="replayDraft" :disabled="draftLocked" :selecting="selecting" :selected="selected" @change="changeDraft" @remove="removeOperation" @toggle="toggleOperation" />
          </template>
          <div class="field">
            <span>测试目标 URL</span>
            <UrlPrefixPicker v-model:address="origin" :model-value="urlPrefix" :disabled="running"
              label="测试目标 URL" placeholder="http://127.0.0.1:9100" @update:model-value="selectUrlPrefix" @submit="submit" />
          </div>
          <p class="hint">文档中的测试基址会自动填入，请确认地址后开始测试。</p>
          <details class="settings"><summary>设置</summary>
            <TestSettingsFields group="task" :disabled="running" :generation-locked="draftLocked || editing" />
          </details>
          <div class="actions"><button class="btn btn-primary" :disabled="(catalog ? !total : !replaySource) || !targetBase || draftLocked || editing || selecting" @click="submit"><AppIcon name="play" />{{ replaySource ? '再次测试' : '开始测试' }}</button><button v-if="running" class="btn" :disabled="busy || run?.stage === 'summarizing'" @click="cancel">取消任务</button></div>
        </section>
      </template>
      <template #results>
        <section class="task-panel scroll-y">
          <h2>执行结果 <span class="result-actions">
            <a v-if="run && !resultsCleared" class="btn btn-sm" :href="'/api/tasks/' + run.id + '/report'" download><AppIcon name="download" />导出总结</a>
            <ResponseDetails :key="run?.id" v-if="run && !resultsCleared" :data="run" note="这里展示当前任务和已记录的用例响应。响应正文或事件可能按记录上限截断。" />
            <button type="button" class="btn btn-ghost btn-sm" title="清除当前执行结果显示" :disabled="!run || resultsCleared" @click="clearResults">清除</button>
          </span></h2>
          <p v-if="!run || resultsCleared" class="hint">{{ resultsCleared && run ? COPY.feedback.clearedCurrentResult : COPY.empty.taskResults }}</p>
          <template v-else>
            <div class="run-overview" role="status">
              <div class="run-meta">
                <span v-if="progress" class="tag status-dot" :class="`tag-${progress.tone}`">{{ progress.label }}</span>
                <span><strong>{{ run.results.length }} / {{ run.snapshot.cases.length }}</strong> 条已记录</span>
                <span>{{ run.stage === 'summarizing' ? '正在生成总结' : '内置契约引擎' }}</span>
              </div>
              <div v-if="streamWait || outcomes.length" class="outcomes" aria-label="用例结果统计">
                <span v-if="streamWait" class="tag tag-info verdict">{{ streamWait.label }} <strong>1</strong></span>
                <span v-for="outcome in outcomes" :key="outcome.status" class="tag verdict" :class="`tag-${outcome.tone}`">
                  <span aria-hidden="true">{{ outcome.symbol }}</span>{{ outcome.label }} <strong>{{ outcome.count }}</strong>
                </span>
              </div>
            </div>
            <p v-if="tlsFailure" class="warning">目标 HTTPS 连接失败。若服务使用 HTTP，可将地址填回表单后重新测试。
              <button type="button" class="btn btn-sm" :disabled="running" @click="useHttpTarget">填入 HTTP 地址</button>
            </p>
            <p v-if="run.error_kind" class="warning">{{ run.error_kind }}；已记录结果仍可导出。</p>
            <TaskResultTree :key="run.id" :task="run" :stream-wait="streamWait" :live-events="liveEvents" :live-events-seen="liveEventsSeen" />
            <TaskSummary :text="run.summary" :pending="running && run.stage === 'summarizing'"
              :status="run.stage === 'summarizing' ? '正在生成总结…' : running ? '等待测试执行完成。' : '暂无 AI 总结，请查看逐条结果。'" />
          </template>
        </section>
      </template>
      <template #assistant>
      <TaskChat scope="tasks" :conversation-key="activeChatHistoryId ?? (run ? run.id : chatDraftKey)" :task-id="resultsCleared ? undefined : run?.id" @busy="chatting = $event" @history-created="createChatHistory" @history-titled="titleChatHistoryRecord" />
      </template>
  </TestWorkspace>
</template>
<style scoped>
.task-panel { height: 100%; min-height: 0; padding: 18px; box-sizing: border-box; }
h2 { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 15px; margin: 0 0 16px; }
.result-actions { display: flex; align-items: center; gap: 6px; }
.field { display: flex; flex-direction: column; gap: 8px; margin: 16px 0; }
.panel-title { display: inline-flex; align-items: center; gap: 8px; }
.panel-title svg { color: var(--accent); }
.upload { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 6px 8px; border: 1px solid var(--accent-border); border-radius: var(--radius-md); background: var(--bg-subtle); }
.upload:focus-within { border-color: var(--accent); }
.document-name { flex: 1 1 120px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.upload .btn { flex: none; }
.draft-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.settings > summary { cursor: pointer; }
.settings > :not(summary) { margin-top: 8px; }
.hint { line-height: 1.7; color: var(--text-tertiary); }
.actions { display: flex; gap: 8px; margin-top: 20px; }
.run-overview { display: grid; gap: 10px; margin-bottom: 12px; padding: 12px; background: var(--bg-subtle); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }
.run-meta, .outcomes { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; color: var(--text-secondary); font-size: 12px; }
.run-meta strong { color: var(--text-primary); }
.warning { color: var(--warning); }
</style>
