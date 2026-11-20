<script setup lang="ts">
/** HTTP 调试页面：请求、历史和响应各有独立工作区。目标请求由后端执行；
 * 历史正文是预览，管理接口错误与目标响应分别展示。 */

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  ArrowUpRight,
  ArrowDownLeft,
  Braces,
  Clock3,
  FileCode2,
  Loader2,
  Send,
  Trash2,
} from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import ResponseDetails from '@/components/ResponseDetails.vue'
import TaskChat from '@/components/TaskChat.vue'
import HistoryList, { type HistoryEntry } from '@/components/HistoryList.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import UrlPrefixPicker from '@/components/UrlPrefixPicker.vue'
import { useHeaderRows } from '@/composables/useHeaderRows'
import EmptyState from '@/components/EmptyState.vue'
import { ApiError, describeError } from '@/api/client'
import {
  clearConsoleHistory,
  deleteConsoleHistory,
  listConsoleHistory,
  readConsoleHistory,
  sendConsoleRequest,
} from '@/api/endpoints'
import type {
  BodyMode,
  ConsoleHistory,
  ConsoleRequestInput,
  ConsoleResult,
  ConsoleSseEvent,
  HttpMethod,
} from '@/api/types'
import {
  describeRedirect,
  formatBytes,
  formatDuration,
  formatRelative,
  isSseHistory,
  looksLikeJson,
  prettyJson,
  statusTone,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { presentManagementError, presentResult, resultCodeLabel, type ResultPresentation } from '@/lib/results'
import { consoleSettings } from '@/lib/testSettings'
import TestSettingsFields from '@/components/TestSettingsFields.vue'
import { joinTargetUrl, urlSuffix } from '@/lib/urlPrefixes'
import { COPY } from '@/lib/copy'
import { attachChatToExecution, isChatOnly, listChatHistories, removeChatHistory, type ChatHistoryRecord } from '@/lib/chatHistory'

const METHODS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
const BODY_MODES: { value: BodyMode; label: string }[] = [
  { value: 'none', label: 'none' },
  { value: 'json', label: 'JSON' },
  { value: 'text', label: 'Text' },
  { value: 'form', label: 'x-www-form-urlencoded' },
]

/** 一组示例值，让页面打开就能一键跑通，而不是面对空表单猜格式。
 *  URL 用本机回环地址，与后端的范围策略一致。 */
const SAMPLES = [
  { label: 'GET 健康检查', method: 'GET' as HttpMethod, url: 'http://127.0.0.1:8010/health', body: '' },
  { label: 'GET OpenAPI', method: 'GET' as HttpMethod, url: 'http://127.0.0.1:8010/openapi.json', body: '' },
  {
    label: 'POST 示例',
    method: 'POST' as HttpMethod,
    url: 'http://127.0.0.1:8080/api/user/login',
    body: '{\n  "username": "demo",\n  "password": "123"\n}',
  },
]

const form = reactive({
  method: 'GET' as HttpMethod,
  url: 'http://127.0.0.1:8010/health',
  bodyMode: 'none' as BodyMode,
  body: '',
})

/** 头以「行」的形式编辑（Postman/Apifox 的心智模型），折叠规则与长连接页共用一份。 */
const {
  rows: headerRows,
  headers: activeHeaders,
  count: headerCount,
  add: addHeaderRow,
  remove: removeHeaderRow,
} = useHeaderRows()

const tab = ref<'params' | 'headers' | 'body' | 'meta'>('body')
const sending = ref(false)
const urlPrefix = ref('')
const result = ref<ConsoleResult | null>(null)
const liveEvents = ref<ConsoleSseEvent[] | null>(null)
const responseCleared = ref(false)
const detailRequest = ref<ConsoleRequestInput | Pick<ConsoleRequestInput, 'url' | 'method' | 'headers' | 'body_mode' | 'body'> | null>(null)
let activeRequest: AbortController | undefined
let historyRead: AbortController | undefined
/** 页面管理链路失败仅保留本地提示，不写入目标结果或历史。 */
const localFailure = ref<ResultPresentation | null>(null)
const history = ref<ConsoleHistory[]>([])
const historyLoading = ref(false)
const restoringHistory = ref(false)
const activeHistoryId = ref<string | null>(null)
const chatDraftKey = ref(`draft-${Date.now()}`)
const chatting = ref(false)
const chatOnlyHistory = ref<ChatHistoryRecord[]>(listChatHistories('console'))

/** 根据所选方法判断是否展示正文能力；GET、HEAD 与 OPTIONS 不允许发送正文。 */
const hasBody = computed(() => !['GET', 'HEAD', 'OPTIONS'].includes(form.method))

/** GET/HEAD/OPTIONS 不允许携带请求体（后端会 422），所以切到这些方法时自动收敛。 */
watch(
  () => form.method,
  (method) => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && form.bodyMode === 'none') {
      form.bodyMode = 'json'
    }
    if (['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      form.bodyMode = 'none'
      tab.value = tab.value === 'body' ? 'headers' : tab.value
    }
  },
  { flush: 'sync' },
)

/** 响应体展示：JSON 自动美化，其它类型原样展示。 */
const responseBodyText = computed(() => {
  const body = result.value?.response?.body
  if (body == null) return ''
  return looksLikeJson(body) ? prettyJson(body) : body
})

/** 实时事件和最终结果共用同一列表，流结束时保留正文滚动容器。 */
const responseEvents = computed(() => result.value?.response?.sse?.events ?? liveEvents.value)
const responseHeaders = computed(() => Object.entries(result.value?.response?.headers ?? {}))
const responseDetails = computed(() => {
  if (responseCleared.value || !detailRequest.value) return null
  if (result.value) return { ...result.value, request: { ...result.value.request, ...detailRequest.value } }
  if (localFailure.value) return { request: detailRequest.value, response: null, error: localFailure.value }
  if (liveEvents.value) return { request: detailRequest.value, response: { sse: { events: liveEvents.value } }, pending: sending.value }
  return null
})

/** 仅按已返回目标响应的 HTTP 状态计算色调；没有响应时为 neutral。 */
const responseTone = computed(() => statusTone(result.value?.response?.status))
/** 即时与历史结果共用分类，不把本地 504 写进目标响应。 */
const resultInfo = computed(() => result.value ? presentResult(result.value, result.value.response?.status) : null)

/**
 * 目标返回 3xx 且没有跟随时，提取它的 Location 供界面提示。
 *
 * 作用：默认不跟随跳转是安全设计（避免被带到授权范围外），但用户只看到一个
 * 302 会以为请求失败或没生效。这里明确告诉他“跳转被刻意拦下了、指向哪里”。
 * 实现：只在响应状态是 3xx 且 ``redirects`` 为空（即未跟随）时返回；否则为 null。
 */
const blockedRedirect = computed(() => {
  const response = result.value?.response
  if (!response || response.status < 300 || response.status >= 400) return null
  if (response.redirects.length) return null
  const location = response.headers['location']?.[0] ?? null
  return { status: response.status, reason: response.reason, location }
})

/** 将示例方法和地址填入表单；仅示例包含正文时切换为 JSON 并覆盖正文，不自动发送。 */
function applySample(sample: (typeof SAMPLES)[number]) {
  form.method = sample.method
  urlPrefix.value = ''
  form.url = sample.url
  if (sample.body) {
    form.bodyMode = 'json'
    form.body = sample.body
    tab.value = 'body'
  }
}

/** Keep the path when switching prefixes and restore the full address on deselection. */
function selectUrlPrefix(prefix: string) {
  form.url = prefix ? urlSuffix(form.url, prefix, urlPrefix.value) : joinTargetUrl(urlPrefix.value, form.url) || form.url
  urlPrefix.value = prefix
}

/** 对合法 JSON 正文进行美化；非法文本保留原状并给出提示。 */
function formatBody() {
  if (!looksLikeJson(form.body)) {
    useToastStore().push('warning', '当前内容不是合法 JSON', '格式化只在内容是 JSON 时可用')
    return
  }
  form.body = prettyJson(form.body)
}

/** 用当前全局设置发送表单请求，显示响应并刷新历史；空地址拦截，目标传输失败与管理接口拒绝分别提示，始终解除 sending。 */
async function send() {
  if (sending.value || restoringHistory.value) return
  const url = joinTargetUrl(urlPrefix.value, form.url)
  if (!url) {
    useToastStore().push('warning', urlPrefix.value ? '请填写有效的 URL 后缀' : '请先填写请求地址')
    return
  }
  sending.value = true
  const sourceChatId = activeHistoryId.value && isChatOnly(activeHistoryId.value) ? activeHistoryId.value : null
  responseCleared.value = false
  const controller = new AbortController()
  activeRequest = controller
  result.value = null
  liveEvents.value = null
  localFailure.value = null
  const payload: ConsoleRequestInput = {
    method: form.method,
    url,
    headers: { ...activeHeaders.value },
    body_mode: form.bodyMode,
    body: form.bodyMode === 'none' ? null : form.body,
    timeout_ms: consoleSettings.timeoutMs,
    sse_max_events: consoleSettings.sseMaxEvents,
    sse_duration_ms: consoleSettings.sseDurationMs,
    follow_redirects: consoleSettings.followRedirects,
  }
  detailRequest.value = payload
  try {
    const completed = await sendConsoleRequest(payload, frame => {
      if (controller.signal.aborted || activeRequest !== controller || responseCleared.value) return
      const events = (liveEvents.value ??= [])
      if (frame.type === 'sse_event') events.push(frame.event)
    }, controller.signal)
    if (controller.signal.aborted) return
    if (activeRequest === controller && completed.history_id) {
      if (sourceChatId) attachChatToExecution('console', sourceChatId, completed.history_id)
      activeHistoryId.value = completed.history_id
      chatOnlyHistory.value = listChatHistories('console')
    }
    if (activeRequest === controller && !responseCleared.value) {
      result.value = completed
      if (completed.ok) {
        liveEvents.value = null
        if (!completed.response?.sse) tab.value = 'body'
      } else {
        const feedback = presentResult(completed)
        useToastStore().push(feedback.tone === 'warning' ? 'warning' : 'danger', feedback.title, feedback.message)
      }
    }
    await loadHistory()
  } catch (error) {
    if (controller.signal.aborted || activeRequest !== controller || responseCleared.value) return
    localFailure.value = presentManagementError(error instanceof ApiError ? error.code : '', error instanceof ApiError ? error.status : 0)
    useToastStore().push(localFailure.value.tone === 'neutral' ? 'info' : localFailure.value.tone, localFailure.value.title, describeError(error))
  } finally {
    if (activeRequest === controller) {
      activeRequest = undefined
      sending.value = false
    }
  }
}

onBeforeUnmount(() => { activeRequest?.abort(); historyRead?.abort() })

/** Clear the current display while allowing an in-flight request to finish and save its history. */
function clearResponse() {
  responseCleared.value = true
  detailRequest.value = null
  result.value = null
  liveEvents.value = null
  localFailure.value = null
  activeHistoryId.value = null
}

/** Open a blank request while any previous response finishes saving to history. */
function newTest() {
  activeRequest = undefined
  sending.value = false
  historyRead?.abort()
  historyRead = undefined
  restoringHistory.value = false
  clearResponse()
  chatOnlyHistory.value = listChatHistories('console')
  chatDraftKey.value = `draft-${Date.now()}`
  form.method = 'GET'
  form.url = ''
  form.bodyMode = 'none'
  form.body = ''
  headerRows.value = [{ name: '', value: '' }]
  urlPrefix.value = ''
  tab.value = 'body'
}

/** 读取最近 HTTP 历史并更新加载态；失败清空列表但不影响主请求流程。 */
async function loadHistory() {
  historyLoading.value = true
  try {
    const page = await listConsoleHistory(30)
    history.value = page.items
    chatOnlyHistory.value = listChatHistories('console')
  } catch {
    // 历史加载失败不影响主流程，界面在历史区显示空态即可。
    history.value = []
  } finally {
    historyLoading.value = false
  }
}

/** 回填历史的请求参数和脱敏后的头，不覆盖全局设置。 */
function restore(record: ConsoleHistory) {
  activeRequest?.abort()
  activeRequest = undefined
  sending.value = false
  responseCleared.value = false
  liveEvents.value = null
  localFailure.value = null
  activeHistoryId.value = record.id
  chatDraftKey.value = record.id
  form.method = record.method as HttpMethod
  urlPrefix.value = ''
  form.url = record.url
  form.bodyMode = record.body_mode as BodyMode
  form.body = record.request_body ?? record.body_preview ?? ''
  detailRequest.value = {
    method: form.method,
    url: record.url,
    headers: record.request_headers as Record<string, string>,
    body_mode: form.bodyMode,
    body: record.request_body ?? record.body_preview,
  }
  const rows = Object.entries(record.request_headers).map(([name, value]) => ({
    name,
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }))
  headerRows.value = rows.length ? rows : [{ name: '', value: '' }]
  result.value = record.ok
    ? {
        ok: true,
        request: { method: record.method, url: record.url },
        response: {
          status: record.status_code ?? 0,
          reason: null,
          http_version: null,
          headers: (record.response_headers ?? {}) as Record<string, string[]>,
          body: record.response_preview,
          body_bytes: record.response_bytes,
          body_truncated: (record.response_preview?.length ?? 0) >= 8192,
          body_encoding: null,
          elapsed_ms: record.elapsed_ms,
          redirects: [],
        },
        error_kind: null,
        error_message: null,
        elapsed_ms: record.elapsed_ms,
      }
    : {
        ok: false,
        request: { method: record.method, url: record.url },
        response: null,
        error_kind: record.error_kind,
        error_message: record.error_message,
        elapsed_ms: record.elapsed_ms,
      }
  result.value.result_code = record.result_code
  result.value.result_status = record.result_status
  const legacyBody = record.body_mode !== 'none' && record.request_body == null && (record.body_preview?.length ?? 0) >= 8192
  useToastStore().push(legacyBody ? 'warning' : 'info', '已回填历史请求', legacyBody
    ? '这条旧记录只有请求体预览，请补全正文后再发送。'
    : '请确认参数后发送，已脱敏的凭据需重新填写。')
}

function createChatHistory(record: ChatHistoryRecord, draftKey: string) {
  chatOnlyHistory.value = [record, ...chatOnlyHistory.value.filter(item => item.id !== record.id)]
  if (chatDraftKey.value === draftKey && activeHistoryId.value === null) activeHistoryId.value = record.id
}

function titleChatHistoryRecord(record: ChatHistoryRecord) {
  chatOnlyHistory.value = [record, ...chatOnlyHistory.value.filter(item => item.id !== record.id)]
}

function selectChatHistory(id: string) {
  const record = chatOnlyHistory.value.find(item => item.id === id && isChatOnly(item.id))
  if (!record) return
  clearResponse()
  activeHistoryId.value = id
  chatDraftKey.value = id
  form.method = 'GET'
  form.url = ''
  form.bodyMode = 'none'
  form.body = ''
  detailRequest.value = null
  headerRows.value = [{ name: '', value: '' }]
  urlPrefix.value = ''
}

function deleteChatHistory(id: string) {
  removeChatHistory('console', id)
  chatOnlyHistory.value = chatOnlyHistory.value.filter(item => item.id !== id)
  if (activeHistoryId.value === id) newTest()
}

/** 删除指定 HTTP 历史，成功后移除本地条目并清理相关选中项；失败提示并保留列表。 */
async function removeHistory(record: ConsoleHistory) {
  try {
    await deleteConsoleHistory(record.id)
    history.value = history.value.filter((item) => item.id !== record.id)
    removeChatHistory('console', record.id)
    chatOnlyHistory.value = chatOnlyHistory.value.filter(item => item.id !== record.id)
    if (activeHistoryId.value === record.id) activeHistoryId.value = null
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 清空服务端 HTTP 历史后同步清理本页列表和选中项；失败提示，不清理当前响应。 */
async function clearAll() {
  try {
    await clearConsoleHistory()
    history.value = []
    for (const chat of chatOnlyHistory.value) removeChatHistory('console', chat.id)
    chatOnlyHistory.value = []
    activeHistoryId.value = null
    useToastStore().push('success', '历史已清空')
  } catch (error) {
    useToastStore().push('danger', '清空失败', describeError(error))
  }
}

/** 把请求记录映射成共享历史列的条目（方法名留给 prefix 插槽渲染）。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  [...history.value.map((record) => ({
    id: record.id,
    label: record.url,
    status: record.ok ? String(record.status_code) : presentResult(record).title,
    tone: record.ok ? statusTone(record.status_code) : presentResult(record).tone,
    meta: `${formatRelative(record.created_at)} · ${formatDuration(record.elapsed_ms)}`,
    raw: record,
    at: record.created_at,
  })), ...chatOnlyHistory.value.filter(chat => chat.id.startsWith('chat-')).map(chat => ({
    id: chat.id,
    label: chat.title,
    status: '仅 AI 对话',
    tone: 'info',
    meta: formatRelative(chat.created_at),
    raw: chat,
    at: chat.created_at,
  }))].sort((first, second) => (second.at ?? '').localeCompare(first.at ?? '')),
)

/** Fetch one complete input. A late detail response must not overwrite a newer selection. */
async function restoreById(id: string) {
  if (sending.value) {
    useToastStore().push('info', '请等待当前请求结束后再切换历史')
    return
  }
  if (isChatOnly(id) && chatOnlyHistory.value.some(item => item.id === id)) {
    selectChatHistory(id)
    return
  }
  historyRead?.abort()
  const controller = new AbortController()
  historyRead = controller
  restoringHistory.value = true
  try {
    const record = await readConsoleHistory(id, controller.signal)
    if (!controller.signal.aborted) restore(record)
  } catch (error) {
    if (!controller.signal.aborted) useToastStore().push('danger', '读取历史失败', describeError(error))
  } finally {
    if (historyRead === controller) {
      historyRead = undefined
      restoringHistory.value = false
    }
  }
}

/** 按共享历史列表提供的 ID 发起删除；记录未加载时忽略，异步失败由 removeHistory 提示。 */
function removeHistoryById(id: string): void {
  if (isChatOnly(id) && chatOnlyHistory.value.some(item => item.id === id)) {
    deleteChatHistory(id)
    return
  }
  const target = history.value.find((item) => item.id === id)
  if (target) removeHistory(target)
}

onMounted(loadHistory)
</script>

<template>
  <TestWorkspace history-key="api-agent.layout.console.history" editor-key="api-agent.layout.console.request-width"
    assistant-key="api-agent.layout.console.chat" :history-width="236" :editor-width="460" hide-history-on-narrow>
    <!-- 左栏：历史请求，与 apifox 的「快捷请求」列表同位 -->
    <template #history>
      <HistoryList
        title="请求历史"
        :items="historyEntries"
        :active-id="activeHistoryId"
        empty-text="还没有请求记录。发送一次请求后，这里会保留脱敏摘要。"
        :on-delete="removeHistoryById"
        :on-clear="clearAll"
        :on-new-test="newTest"
        @select="restoreById"
      >
        <template #prefix="{ item }">
          <span class="method" :class="`method-${String((item.raw as ConsoleHistory | undefined)?.method ?? '').toLowerCase()}`">
            {{ (item.raw as ConsoleHistory | undefined)?.method }}
          </span>
          <span v-if="isSseHistory(item.raw as ConsoleHistory | undefined)" class="tag tag-accent">SSE</span>
        </template>
      </HistoryList>
    </template>

      <template #editor>
        <section class="workbench scroll-y">
      <header class="request-head">
        <h1 class="panel-title"><ArrowUpRight :size="15" aria-hidden="true" /> 请求</h1>
        <span class="protocol-label">HTTP</span>
      </header>
      <div class="url-bar">
        <UrlPrefixPicker v-model:address="form.url" :model-value="urlPrefix" label="请求地址"
          placeholder="http://127.0.0.1:8080/api/..." @update:model-value="selectUrlPrefix" @submit="send">
          <template #leading>
            <select v-model="form.method" class="method-select method" :class="`method-${form.method.toLowerCase()}`" aria-label="HTTP 方法">
              <option v-for="method in METHODS" :key="method" :value="method">{{ method }}</option>
            </select>
          </template>
        </UrlPrefixPicker>
        <button class="btn btn-accent" :disabled="sending || restoringHistory" @click="send">
          <Loader2 v-if="sending" :size="14" class="spinning" />
          <Send v-else :size="14" />
          <span>{{ sending ? '发送中' : '发送' }}</span>
        </button>
      </div>

      <div class="samples">
        <span class="samples-label">示例</span>
        <button
          v-for="sample in SAMPLES"
          :key="sample.label"
          class="sample-chip"
          @click="applySample(sample)"
        >
          {{ sample.label }}
        </button>
      </div>

      <div class="tabs">
        <button
          v-for="item in [
            { key: 'body', label: '请求体' },
            { key: 'headers', label: `请求头 ${headerCount || ''}`.trim() },
            { key: 'meta', label: '设置' },
          ]"
          :key="item.key"
          class="tab"
          :class="{ 'tab-active': tab === item.key }"
          @click="tab = item.key as typeof tab"
        >
          {{ item.label }}
        </button>
        <div class="tabs-fill" />
        <span class="send-hint hint"><kbd>Enter</kbd> 发送</span>
      </div>

      <div v-if="tab === 'body'" class="tab-body">
        <div class="body-modes">
          <button
            v-for="mode in BODY_MODES"
            :key="mode.value"
            class="mode-chip"
            :class="{ 'mode-chip-active': form.bodyMode === mode.value }"
            :disabled="!hasBody && mode.value !== 'none'"
            @click="form.bodyMode = mode.value"
          >
            {{ mode.label }}
          </button>
          <div class="tabs-fill" />
          <button
            v-if="looksLikeJson(form.body)"
            class="btn btn-ghost btn-sm"
            @click="formatBody"
          >
            <Braces :size="13" />
            格式化
          </button>
        </div>
        <p v-if="!hasBody" class="hint body-hint">
          {{ form.method }} 不支持请求体，后端会拒绝携带 body 的该类请求。
        </p>
        <textarea
          v-model="form.body"
          class="textarea mono body-editor"
          spellcheck="false"
          :disabled="form.bodyMode === 'none'"
          :placeholder="form.bodyMode === 'none' ? '选择一种请求体类型后开始编辑' : '在此粘贴请求体'"
        />
      </div>

      <div v-else-if="tab === 'headers'" class="tab-body">
        <div class="header-table">
          <div class="header-row header-row-head">
            <span>名称</span>
            <span>值</span>
            <span />
          </div>
          <div v-for="(row, index) in headerRows" :key="index" class="header-row">
            <input v-model="row.name" class="input mono" placeholder="Content-Type" spellcheck="false" />
            <input v-model="row.value" class="input mono" placeholder="application/json" spellcheck="false" />
            <button class="btn btn-ghost btn-icon btn-sm" title="删除这一行" @click="removeHeaderRow(index)">
              <Trash2 :size="13" />
            </button>
          </div>
        </div>
        <button class="btn btn-sm add-header" @click="addHeaderRow">+ 添加请求头</button>
        <p class="hint">
          Host、Content-Length、Connection 等由传输层管理的头不允许覆盖；
          凭据类头会以
          <code>[redacted]</code> 的形式保存到历史。
        </p>
      </div>

      <div v-else class="tab-body meta-grid">
        <p class="hint settings-hint">适用于本浏览器的所有接口测试请求，不随历史回填而改变。</p>
        <TestSettingsFields class="settings-fields" group="console" />
        <div class="scope-note">
          <Clock3 :size="14" />
          <div>
            <strong>范围限制</strong>
            <p>
              只允许数字回环地址（127.0.0.1、::1 等）；用域名或内网 IP 会被后端拒绝并返回
              <code>target_not_loopback</code>。
            </p>
          </div>
        </div>
      </div>

        </section>
      </template>
      <template #results>
        <div class="response">
          <div class="response-head">
            <h2 class="panel-title"><ArrowDownLeft :size="15" aria-hidden="true" /> 响应</h2>
            <div class="response-stats">
              <span v-if="sending && !responseCleared" class="tag tag-info" role="status">
                <Loader2 :size="14" class="spinning" aria-hidden="true" />
                {{ responseEvents ? `正在接收 SSE · ${responseEvents.length} 个事件` : '正在等待响应…' }}
              </span>
              <template v-else-if="result">
                <span v-if="result.response" class="tag status-dot" :class="`tag-${responseTone}`">
                  {{ result.response.status }} {{ result.response.reason ?? '' }}
                </span>
                <span v-else class="tag status-dot" :class="`tag-${resultInfo?.tone}`">
                  {{ resultInfo?.title }}<template v-if="resultCodeLabel(resultInfo?.resultStatus)"> · {{ resultCodeLabel(resultInfo?.resultStatus) }}</template>
                </span>
                <span class="tag">{{ formatDuration(result.elapsed_ms) }}</span>
                <span v-if="result.response" class="tag">{{ formatBytes(result.response.body_bytes) }}</span>
                <span v-if="result.response?.sse" class="tag tag-accent">SSE · {{ result.response.sse.events.length }} 事件 · {{ result.response.sse.stop_reason }}</span>
                <CopyButton v-if="responseBodyText" :text="responseBodyText" label="复制响应" />
              </template>
              <ResponseDetails :key="detailRequest ? 'response' : 'empty'" :data="responseDetails"
                :note="activeHistoryId ? '历史响应显示当时保存的脱敏数据和正文预览。' : undefined" />
              <button type="button" class="btn btn-ghost btn-sm" title="清除当前响应显示" :disabled="!result && !localFailure && (!sending || responseCleared)" @click="clearResponse">清除</button>
            </div>
          </div>

          <div class="response-split">
            <div class="response-content code-block scroll-y">
              <div v-if="responseEvents !== null" class="sse-events" aria-label="已接收的 SSE 事件">
                <p v-if="!responseEvents.length" class="hint">{{ sending ? '等待首个事件…' : '未收到事件。' }}</p>
                <div v-for="(event, index) in responseEvents" :key="index" class="sse-event">
                  <span class="mini-label">{{ index + 1 }} · {{ event.event }}</span>
                  <pre>{{ event.data }}</pre>
                </div>
              </div>

              <div v-if="localFailure" class="response-error">
                <div class="error-title">{{ localFailure.title }}</div>
                <p class="error-detail">{{ localFailure.message }}</p>
                <p class="hint">结果码：<code>{{ localFailure.code }}</code><template v-if="localFailure.resultStatus != null">；管理接口 HTTP {{ localFailure.resultStatus }}</template></p>
              </div>
              <div v-else-if="!result && (!sending || responseCleared) && responseEvents === null" class="response-empty">
                <EmptyState
                  :icon="FileCode2"
                  title="还没有响应"
                  :description="COPY.empty.consoleResponse"
                />
              </div>

              <div v-else-if="result && !result.response" class="response-error">
                <div class="error-title">{{ resultInfo?.title }}</div>
                <p class="error-detail">{{ resultInfo?.message }}</p>
                <p class="hint">
                  结果码：<code>{{ resultInfo?.code }}</code>；错误类别：<code>{{ result.error_kind }}</code>。
                  这表示本服务没能取得完整、可评估的 HTTP 响应，
                  不代表目标返回了某个错误状态码。
                </p>
              </div>

              <template v-if="result?.response">
                <div v-if="result.response.body_truncated" class="truncated-note">
                  {{ result.response.sse ? '仅显示已观察到的 SSE 内容。' : '响应体已截断展示（显示前 1,000,000 字符）。' }}
                </div>
                <div v-if="blockedRedirect" class="redirect-blocked">
                  <div class="redirect-head">
                    <ArrowUpRight :size="12" />
                    目标返回 <strong>{{ describeRedirect(blockedRedirect.status, blockedRedirect.reason) }}</strong>，默认不跟随跳转
                  </div>
                  <div v-if="blockedRedirect.location" class="redirect-hop">
                    <span class="tag tag-info">{{ blockedRedirect.status }}</span>
                    <span class="hop-arrow">→</span>
                    <span class="hop-url">{{ blockedRedirect.location }}</span>
                  </div>
                  <div class="redirect-hint">如需跟随，在「设置」中开启「跟随重定向」。跳转仍会逐跳校验目标范围。</div>
                </div>
                <div v-if="result.response.redirects.length" class="redirect-note">
                  <div class="redirect-head">已跟随跳转：</div>
                  <div v-for="(hop, index) in result.response.redirects" :key="index" class="redirect-hop">
                    <span class="tag" :class="`tag-${statusTone(hop.status)}`">
                      {{ describeRedirect(hop.status, hop.reason) }}
                    </span>
                    <span class="hop-arrow">→</span>
                    <span class="hop-url">{{ hop.url }}</span>
                    <span v-if="hop.cross_origin" class="tag tag-warning">跨源 · 已剥离凭据</span>
                  </div>
                  <div class="redirect-hop">
                    <span class="tag" :class="`tag-${statusTone(result.response.status)}`">
                      {{ describeRedirect(result.response.status, result.response.reason) }}
                    </span>
                    <span class="hop-final">最终响应</span>
                  </div>
                </div>
                <pre v-if="responseEvents === null" class="raw-response">{{ responseBodyText || '（空响应体）' }}</pre>
              </template>
            </div>
            <section class="response-headers" aria-label="响应头">
              <div class="mini-label">响应头</div>
              <div class="response-header-list scroll-y">
                <p v-if="!responseHeaders.length" class="hint">{{ sending && !responseCleared ? '等待响应头…' : '暂无响应头。' }}</p>
                <div v-for="([name, values]) in responseHeaders" :key="name" class="header-line">
                  <span class="header-name">{{ name }}</span>
                  <span class="header-value">{{ values.join(', ') }}</span>
                </div>
              </div>
            </section>
          </div>
        </div>
      </template>
    <template #assistant>
      <TaskChat scope="console" :conversation-key="activeHistoryId ?? chatDraftKey" @busy="chatting = $event"
        @history-created="createChatHistory" @history-titled="titleChatHistoryRecord" />
    </template>
  </TestWorkspace>
</template>

<style scoped>

/* ---------- 左：历史 ---------- */
.tone-success { color: var(--success); }
.tone-info { color: var(--info); }
.tone-warning { color: var(--warning); }
.tone-danger { color: var(--danger); }
.tone-neutral { color: var(--text-tertiary); }

/* ---------- 右：工作台 ---------- */
.workbench {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
  /* 请求编辑区使用统一主题表面，保持分栏边缘连续。 */
  background: var(--bg-surface);
}

.request-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex: none;
  padding: 14px 20px 0;
}

.panel-title {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0;
  font-size: 14px;
  font-weight: 650;
}

.panel-title svg { color: var(--text-tertiary); }

.protocol-label {
  padding: 2px 6px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  font: 10px/1.3 var(--font-mono);
  letter-spacing: 0.06em;
}

.url-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px 10px;
  flex: none;
}

.method-select {
  width: 91px;
  flex: none;
  border: 0;
  border-right: 1px solid var(--border-subtle);
  border-radius: calc(var(--radius-md) - 1px) 0 0 calc(var(--radius-md) - 1px);
  background: var(--bg-subtle);
  font-size: 12px;
  padding: 0 9px;
}

.method-select:focus-visible { outline-offset: -2px; }

.url-bar .btn {
  height: 38px;
  padding: 0 16px;
}

.samples {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 20px 10px;
  flex: none;
  flex-wrap: wrap;
}

.samples-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.sample-chip {
  height: 24px;
  padding: 0 7px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  color: var(--text-secondary);
  font-size: 11.5px;
}

.sample-chip:hover {
  border-color: var(--accent-border);
  color: var(--accent);
  background: var(--accent-soft);
}

.tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.tab-active {
  font-weight: 600;
}

.send-hint { white-space: nowrap; }
.send-hint kbd {
  padding: 1px 4px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  font: 10px var(--font-mono);
}

.tabs-fill {
  flex: 1;
}

.tab-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  padding: 12px 20px;
}

.body-modes {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  margin-bottom: 9px;
}

.mode-chip {
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 11.5px;
}

.mode-chip:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.mode-chip-active {
  background: var(--accent-soft);
  border-color: var(--accent-border);
  color: var(--accent);
}

.mode-chip:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.body-editor {
  flex: 1;
  min-height: 100px;
  background: var(--bg-sunken);
}

.body-hint {
  margin: 0 0 8px;
}

.header-table {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.header-row {
  display: grid;
  grid-template-columns: 1fr 1.4fr 28px;
  gap: 6px;
  align-items: center;
}

.header-row-head {
  font-size: 11px;
  color: var(--text-tertiary);
  padding: 0 2px;
}

.header-row .input {
  height: 30px;
}

.add-header {
  margin-top: 9px;
}

.meta-grid {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 14px 20px;
  align-items: start;
}

.settings-hint {
  grid-column: 1 / -1;
  margin: 0;
}

.settings-fields { grid-column: 1 / -1; }

.switch-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.switch {
  width: 15px;
  height: 15px;
  margin-top: 2px;
  accent-color: var(--accent);
}

.scope-note {
  grid-column: 1 / -1;
  display: flex;
  gap: 9px;
  padding: 11px 13px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}

.scope-note p {
  margin: 3px 0 0;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-tertiary);
}

.scope-note strong {
  font-size: 12px;
}

/* ---------- 响应 ---------- */
.response {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  /* 不再画上边框：与请求区同底色，靠分隔条的手柄提示可调，避免双层浅灰线。 */
  background: var(--bg-surface);
}

.response-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border-subtle);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-rail);
  flex: none;
}

.response-head .panel-title { flex: none; }

.response-stats {
  display: flex;
  align-items: center;
  justify-content: safe flex-end;
  min-width: 0;
  height: 36px;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: thin;
  gap: 7px;
}

.response-stats > :deep(*) {
  flex: none;
  white-space: nowrap;
}

.response-empty {
  display: grid;
  align-items: center;
  flex: 1;
}

.response-empty :deep(.empty-icon) {
  margin-bottom: 5px;
  box-shadow: 0 0 0 6px var(--bg-rail), 0 0 0 7px var(--border-subtle);
}

.response-error {
  padding: 20px 16px;
}

.error-title {
  font-size: 13px;
  font-weight: 650;
  color: var(--danger);
}

.error-detail {
  margin: 6px 0 8px;
  font-size: 12px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  word-break: break-word;
}

.response-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sse-event + .sse-event { border-top: 1px solid var(--border-subtle); }
.sse-event pre, .raw-response { font-family: var(--font-mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.sse-event pre { margin: 4px 0 8px; }
.raw-response { margin: 0; }

.truncated-note,
.redirect-note {
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 11.5px;
}

.redirect-note {
  background: var(--info-soft);
  color: var(--info);
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
}

/* 未跟随的 3xx：独立提示，说明跳转被刻意拦下而不是失败。 */
.redirect-blocked {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--info-soft);
  color: var(--info);
  font-size: 11.5px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  border-left: 2px solid var(--info);
}

.redirect-head {
  display: flex;
  align-items: center;
  gap: 5px;
  font-weight: 600;
}

.redirect-hint {
  color: var(--text-tertiary);
  font-size: 11px;
}

.redirect-hop {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 11.5px;
}

.hop-arrow {
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}

.hop-url {
  font-family: var(--font-mono);
  color: var(--text-secondary);
  word-break: break-all;
}

.hop-final {
  color: var(--text-tertiary);
  font-size: 11px;
}

.hop {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-family: var(--font-mono);
}

.response-split {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 290px;
  grid-template-rows: minmax(0, 1fr);
  padding: 12px 20px 16px;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.code-block {
  margin: 0;
  padding: 11px 12px;
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  overflow: auto;
}

.response-headers {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  padding: 2px 0;
}

.response-header-list { min-height: 0; overflow: auto; }
.response-header-list .hint { margin: 0; }

.mini-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
  margin-bottom: 6px;
}

.header-line {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 5px 0;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
}

.header-name {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--text-secondary);
}

.header-value {
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  word-break: break-all;
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1180px) {
  .response-split {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr) minmax(54px, 30%);
  }
}

@media (max-width: 680px) {
  .request-head, .url-bar, .samples, .tabs, .tab-body, .response-head, .response-split {
    padding-inline: 12px;
  }
  .url-bar .btn { padding: 0 10px; }
  .send-hint { display: none; }
  .panel-title { white-space: nowrap; }
}
</style>
