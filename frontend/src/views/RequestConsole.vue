<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  ArrowUpRight,
  Braces,
  Clock3,
  FileCode2,
  Loader2,
  Send,
  Trash2,
} from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import HistoryList, { type HistoryEntry } from '@/components/HistoryList.vue'
import ResizableSplit from '@/components/ResizableSplit.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import { useHeaderRows } from '@/composables/useHeaderRows'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import {
  clearConsoleHistory,
  deleteConsoleHistory,
  listConsoleHistory,
  sendConsoleRequest,
} from '@/api/endpoints'
import type {
  BodyMode,
  ConsoleHistory,
  ConsoleResult,
  HttpMethod,
} from '@/api/types'
import {
  formatBytes,
  formatDuration,
  formatRelative,
  looksLikeJson,
  prettyJson,
  statusTone,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

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
  timeoutMs: 10_000,
  followRedirects: false,
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
const result = ref<ConsoleResult | null>(null)
const history = ref<ConsoleHistory[]>([])
const historyLoading = ref(false)
const activeHistoryId = ref<string | null>(null)

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
)

/** 响应体展示：JSON 自动美化，其它类型原样展示。 */
const responseBodyText = computed(() => {
  const body = result.value?.response?.body
  if (body == null) return ''
  return looksLikeJson(body) ? prettyJson(body) : body
})

const responseTone = computed(() => statusTone(result.value?.response?.status))

function applySample(sample: (typeof SAMPLES)[number]) {
  form.method = sample.method
  form.url = sample.url
  if (sample.body) {
    form.bodyMode = 'json'
    form.body = sample.body
    tab.value = 'body'
  }
}

function formatBody() {
  if (!looksLikeJson(form.body)) {
    useToastStore().push('warning', '当前内容不是合法 JSON', '格式化只在内容是 JSON 时可用')
    return
  }
  form.body = prettyJson(form.body)
}

async function send() {
  if (!form.url.trim()) {
    useToastStore().push('warning', '请先填写请求地址')
    return
  }
  sending.value = true
  activeHistoryId.value = null
  try {
    result.value = await sendConsoleRequest({
      method: form.method,
      url: form.url.trim(),
      headers: activeHeaders.value,
      body_mode: form.bodyMode,
      body: form.bodyMode === 'none' ? null : form.body,
      timeout_ms: form.timeoutMs,
      follow_redirects: form.followRedirects,
    })
    if (result.value.ok) {
      tab.value = 'body'
    } else {
      useToastStore().push(
        result.value.error_kind === 'request_timeout' ? 'warning' : 'danger',
        result.value.error_kind === 'request_timeout' ? '请求超时' : '请求未送达',
        result.value.error_message ?? undefined,
      )
    }
    await loadHistory()
  } catch (error) {
    useToastStore().push('danger', '请求被拒绝', describeError(error))
  } finally {
    sending.value = false
  }
}

async function loadHistory() {
  historyLoading.value = true
  try {
    const page = await listConsoleHistory(30)
    history.value = page.items
  } catch {
    // 历史加载失败不影响主流程，界面在历史区显示空态即可。
    history.value = []
  } finally {
    historyLoading.value = false
  }
}

/** 点击历史把当时的请求参数回填到表单（只回填脱敏后的头）。 */
function restore(record: ConsoleHistory) {
  activeHistoryId.value = record.id
  form.method = record.method as HttpMethod
  form.url = record.url
  form.bodyMode = record.body_mode as BodyMode
  form.body = record.body_preview ?? ''
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
  useToastStore().push('info', '已回填历史请求', '响应来自历史记录（正文为预览片段）')
}

async function removeHistory(record: ConsoleHistory) {
  try {
    await deleteConsoleHistory(record.id)
    history.value = history.value.filter((item) => item.id !== record.id)
    if (activeHistoryId.value === record.id) activeHistoryId.value = null
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

async function clearAll() {
  try {
    await clearConsoleHistory()
    history.value = []
    activeHistoryId.value = null
    useToastStore().push('success', '历史已清空')
  } catch (error) {
    useToastStore().push('danger', '清空失败', describeError(error))
  }
}

/** 响应区高度：上下分栏的受控尺寸，持久化到本页专属 key。 */
const responseHeight = ref(320)

function restoreResponseHeight(): number {
  try {
    const saved = Number(window.localStorage.getItem('api-agent.layout.console.response'))
    return Number.isFinite(saved) && saved > 0 ? saved : 320
  } catch {
    return 320
  }
}

responseHeight.value = restoreResponseHeight()

/** 把请求记录映射成共享历史列的条目（方法名留给 prefix 插槽渲染）。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  history.value.map((record) => ({
    id: record.id,
    label: record.url,
    status: record.ok ? String(record.status_code) : record.error_kind ?? 'error',
    tone: statusTone(record.status_code),
    meta: `${formatRelative(record.created_at)} · ${formatDuration(record.elapsed_ms)}`,
    raw: record,
  })),
)

function restoreById(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) restore(target)
}

function removeHistoryById(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) removeHistory(target)
}

onMounted(loadHistory)
</script>

<template>
  <TestWorkspace persist-key="api-agent.layout.console.history" class="console">
    <!-- 左栏：历史请求，与 apifox 的「快捷请求」列表同位 -->
    <template #history>
      <HistoryList
        title="请求历史"
        :items="historyEntries"
        :active-id="activeHistoryId"
        empty-text="还没有请求记录。发送一次请求后，这里会保留脱敏摘要。"
        @select="restoreById"
      >
        <template #head-actions>
          <button
            type="button"
            class="btn btn-ghost btn-sm"
            :disabled="history.length === 0"
            title="清空全部历史"
            @click="clearAll"
          >
            <Trash2 :size="13" />
          </button>
        </template>
        <template #prefix="{ item }">
          <span class="method" :class="`method-${String((item.raw as ConsoleHistory | undefined)?.method ?? '').toLowerCase()}`">
            {{ (item.raw as ConsoleHistory | undefined)?.method }}
          </span>
        </template>
        <template #actions="{ item }">
          <span
            class="history-delete"
            title="删除这条记录"
            @click.stop="removeHistoryById(item.id)"
          >
            <Trash2 :size="12" />
          </span>
        </template>
      </HistoryList>
    </template>

    <!-- 右栏：请求编辑 + 响应（上下可拖动，各自有最小阈值） -->
    <ResizableSplit
      v-model:size="responseHeight"
      direction="vertical"
      persist-key="api-agent.layout.console.response"
      :min="240"
      :min-other="220"
      label="拖动调整请求区与响应区高度"
    >
      <template #first>
        <section class="workbench scroll-y">
      <div class="url-bar">
        <select v-model="form.method" class="method-select" aria-label="HTTP 方法">
          <option v-for="method in METHODS" :key="method" :value="method">{{ method }}</option>
        </select>
        <input
          v-model="form.url"
          class="url-input mono"
          spellcheck="false"
          placeholder="http://127.0.0.1:8080/api/..."
          @keydown.enter.exact.prevent="send"
        />
        <button class="btn btn-accent" :disabled="sending" @click="send">
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
        <span class="hint">Enter 直接发送</span>
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
        <label class="field">
          <span class="field-label">超时（毫秒）</span>
          <input v-model.number="form.timeoutMs" class="input" type="number" min="100" max="60000" step="100" />
          <span class="hint">100 – 60000，超时会被归类为 request_timeout</span>
        </label>
        <label class="field">
          <span class="field-label">跟随重定向</span>
          <span class="switch-row">
            <input v-model="form.followRedirects" type="checkbox" class="switch" />
            <span class="hint">
              默认关闭：目标返回 3xx 时如实显示 302，不会被静默带到授权范围外
            </span>
          </span>
        </label>
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
      <template #second>
        <div class="response">
        <div class="response-head">
          <div class="response-title">返回响应</div>
          <div v-if="result" class="response-stats">
            <span v-if="result.response" class="tag" :class="`tag-${responseTone === 'neutral' ? '' : responseTone}`">
              {{ result.response.status }} {{ result.response.reason ?? '' }}
            </span>
            <span v-else class="tag tag-danger">{{ result.error_kind }}</span>
            <span class="tag">{{ formatDuration(result.elapsed_ms) }}</span>
            <span v-if="result.response" class="tag">
              {{ formatBytes(result.response.body_bytes) }}
            </span>
            <CopyButton v-if="responseBodyText" :text="responseBodyText" label="复制响应" />
          </div>
        </div>

        <div v-if="!result" class="response-empty">
          <EmptyState
            :icon="FileCode2"
            title="还没有响应"
            description="点「发送」获取回包。目标未启动时会得到 request_failed，而不是一个伪造的状态码。"
          />
        </div>

        <div v-else-if="!result.response" class="response-error">
          <div class="error-title">请求未送达目标</div>
          <p class="error-detail">{{ result.error_message }}</p>
          <p class="hint">
            错误类别：<code>{{ result.error_kind }}</code>。这表示本服务没能拿到 HTTP 回包，
            不代表目标返回了某个错误状态码。
          </p>
        </div>

        <div v-else class="response-body">
          <div v-if="result.response.body_truncated" class="truncated-note">
            响应体已截断展示（显示前 1,000,000 字符）。
          </div>
          <div v-if="result.response.redirects.length" class="redirect-note">
            已跟随跳转：
            <span v-for="(hop, index) in result.response.redirects" :key="index" class="hop">
              {{ hop }}<ArrowUpRight :size="11" />
            </span>
          </div>
          <div class="response-split">
            <pre class="code-block scroll-y">{{ responseBodyText || '（空响应体）' }}</pre>
            <div class="response-headers">
              <div class="mini-label">响应头</div>
              <div
                v-for="(values, name) in result.response.headers"
                :key="name"
                class="header-line"
              >
                <span class="header-name">{{ name }}</span>
                <span class="header-value">{{ values.join(', ') }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      </template>
    </ResizableSplit>
  </TestWorkspace>
</template>

<style scoped>
.console {
  height: 100%;
  min-height: 0;
}

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
  /* 与手动用例的编辑区同底色：整块工作台是一个连续白面，不出现色差缝隙。 */
  background: var(--bg-surface);
}

.url-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px 8px;
  flex: none;
}

.method-select {
  width: 108px;
  flex: none;
  height: 34px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  color: var(--method-get);
  padding: 0 8px;
}

.url-input {
  flex: 1;
  min-width: 0;
  height: 34px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  padding: 0 11px;
  outline: none;
}

.url-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.url-bar .btn {
  height: 34px;
}

.samples {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 16px 10px;
  flex: none;
  flex-wrap: wrap;
}

.samples-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.sample-chip {
  height: 22px;
  padding: 0 9px;
  border: 1px solid var(--border-default);
  border-radius: 99px;
  background: var(--bg-surface);
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
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.tab {
  height: 33px;
  padding: 0 11px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12.5px;
  font-weight: 500;
}

.tab:hover {
  color: var(--text-primary);
}

.tab-active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}

.tabs-fill {
  flex: 1;
}

.tab-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  padding: 12px 16px;
}

.body-modes {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-bottom: 9px;
}

.mode-chip {
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: 99px;
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 11.5px;
}

.mode-chip:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.mode-chip-active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--text-inverse);
}

.mode-chip:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.body-editor {
  flex: 1;
  min-height: 132px;
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
  background: var(--bg-surface);
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
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.response-title {
  font-size: 13px;
  font-weight: 650;
}

.response-stats {
  display: flex;
  align-items: center;
  gap: 7px;
}

.response-empty,
.response-error {
  flex: 1;
  overflow: auto;
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

.response-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  padding: 10px 16px 14px;
  gap: 8px;
}

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
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
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
  overflow: auto;
  padding: 2px 0;
}

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
  border-bottom: 1px dashed var(--border-subtle);
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
  .history-pane {
    display: none;
  }

  .response-split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
