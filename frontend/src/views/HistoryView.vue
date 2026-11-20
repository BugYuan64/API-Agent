<script setup lang="ts">
/** 汇总接口、长连接和文档用例测试的持久化结果，选中任务后按需读取完整证据。 */

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ChevronRight, History, Inbox, Radio, Trash2 } from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import EmptyState from '@/components/EmptyState.vue'
import ResizableSplit from '@/components/ResizableSplit.vue'
import TaskResultTree from '@/components/TaskResultTree.vue'
import { describeError } from '@/api/client'
import {
  clearConsoleHistory,
  deleteConsoleHistory,
  listConsoleHistory,
  listStreams,
} from '@/api/endpoints'
import { listTasks, readTask, taskHistoryLabel, type TaskHistory, type TaskRecord } from '@/api/tasks'
import type { ConsoleHistory, StreamHistory } from '@/api/types'
import {
  describeStopReason,
  formatBytes,
  formatDateTime,
  formatDuration,
  isSseHistory,
  prettyJson,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { countCaseOutcomes, presentResult, resultCodeLabel, taskProgress } from '@/lib/results'
import { activeStreamWait } from '@/lib/taskResults'

const tab = ref<'http' | 'stream' | 'task'>('http')
const httpItems = ref<ConsoleHistory[]>([])
const streamItems = ref<StreamHistory[]>([])
const taskItems = ref<TaskHistory[]>([])
const loading = ref(false)
const selectedHttp = ref<ConsoleHistory | null>(null)
const selectedStream = ref<StreamHistory | null>(null)
const selectedTaskId = ref('')
const task = ref<TaskRecord | null>(null)
const taskLoading = ref(false)
const taskOutcomes = computed(() => countCaseOutcomes(task.value?.results ?? []).filter(item => item.count > 0))
const streamWait = computed(() => task.value ? activeStreamWait(task.value) : null)
const viewport = window.matchMedia('(max-width: 760px)')
const narrow = ref(viewport.matches)
const listWidth = ref(360)
const listHeight = ref(190)
let taskRequest: AbortController | undefined

function resizeLayout(event: MediaQueryListEvent) { narrow.value = event.matches }

/** Cancel stale reads after switching tasks so the detail always matches the selected row. */
async function selectTask(id: string) {
  taskRequest?.abort()
  const request = new AbortController()
  taskRequest = request
  if (selectedTaskId.value !== id) task.value = null
  selectedTaskId.value = id
  taskLoading.value = true
  try {
    const record = await readTask(id, request.signal)
    if (!request.signal.aborted) task.value = record
  } catch (error) {
    if (!request.signal.aborted) useToastStore().push('danger', '读取任务失败', describeError(error))
  } finally {
    if (!request.signal.aborted) taskLoading.value = false
  }
}

/** 根据选中 HTTP 历史的状态码计算色调；未选择时使用 neutral。 */
const activeTone = computed(() => (selectedHttp.value ? presentResult(selectedHttp.value, selectedHttp.value.status_code).tone : 'neutral'))

/** 并行读取三类历史，成功来源独立显示，失败来源置空并汇总一条提示；始终解除 loading。 */
async function load() {
  loading.value = true
  try {
    const [http, streams, tasks] = await Promise.allSettled([
      listConsoleHistory(60),
      listStreams(60),
      listTasks(),
    ])
    httpItems.value = http.status === 'fulfilled' ? http.value.items : []
    streamItems.value = streams.status === 'fulfilled' ? streams.value.items : []
    taskItems.value = tasks.status === 'fulfilled' ? tasks.value.items : []
    if (selectedTaskId.value && tab.value === 'task' && tasks.status === 'fulfilled') {
      if (taskItems.value.some(item => item.id === selectedTaskId.value)) void selectTask(selectedTaskId.value)
      else {
        taskRequest?.abort()
        selectedTaskId.value = ''
        task.value = null
        taskLoading.value = false
      }
    }
    const failure = [http, streams, tasks].find((item) => item.status === 'rejected')
    if (failure && failure.status === 'rejected') {
      useToastStore().push('warning', '部分历史加载失败', describeError(failure.reason))
    }
  } finally {
    loading.value = false
  }
}

/** 删除指定 HTTP 历史并同步列表和当前选中项；其他历史来源不受影响，失败提示。 */
async function removeRecord(record: ConsoleHistory) {
  try {
    await deleteConsoleHistory(record.id)
    httpItems.value = httpItems.value.filter((item) => item.id !== record.id)
    if (selectedHttp.value?.id === record.id) selectedHttp.value = null
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 清空服务端 HTTP 历史并重置本页 HTTP 选择；长连接和执行记录由各自来源保留，失败提示。 */
async function clearAll() {
  try {
    await clearConsoleHistory()
    httpItems.value = []
    selectedHttp.value = null
    useToastStore().push('success', 'HTTP 历史已清空')
  } catch (error) {
    useToastStore().push('danger', '清空失败', describeError(error))
  }
}

onMounted(() => { viewport.addEventListener('change', resizeLayout); void load() })
onBeforeUnmount(() => { viewport.removeEventListener('change', resizeLayout); taskRequest?.abort() })
</script>

<template>
  <div class="history-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">执行结果</h2>
      </div>
      <div class="page-actions">
        <button class="btn btn-sm" :disabled="loading" @click="load">刷新</button>
        <button
          v-if="tab === 'http' && httpItems.length"
          class="btn btn-sm btn-danger"
          @click="clearAll"
        >
          <Trash2 :size="12" />
          清空
        </button>
      </div>
    </div>

    <div class="tabs">
      <button
        v-for="item in [
          { key: 'http', label: `HTTP 请求 ${httpItems.length}` },
          { key: 'stream', label: `长连接会话 ${streamItems.length}` },
          { key: 'task', label: `智能群测 ${taskItems.length}` },
        ]"
        :key="item.key"
        class="tab"
        :class="{ 'tab-active': tab === item.key }"
        @click="tab = item.key as typeof tab"
      >
        {{ item.label }}
      </button>
    </div>

    <ResizableSplit :key="Number(narrow)" class="results-split" :direction="narrow ? 'vertical' : 'horizontal'"
      :size="narrow ? listHeight : listWidth" :min="narrow ? 110 : 215" :min-other="narrow ? 180 : 300"
      :persist-key="narrow ? 'api-agent.layout.results.height' : 'api-agent.layout.results.width'"
      label="调整记录列表与详情大小" @update:size="narrow ? listHeight = $event : listWidth = $event">
      <template #first>
      <div v-if="tab === 'http'" class="list scroll-y">
        <button
          v-for="record in httpItems"
          :key="record.id"
          class="row"
          :class="{ 'row-active': selectedHttp?.id === record.id }"
          @click="selectedHttp = record"
        >
          <div class="row-main">
            <div class="row-top">
              <span class="method" :class="`method-${record.method.toLowerCase()}`">{{ record.method }}</span>
              <span v-if="isSseHistory(record)" class="tag tag-accent">SSE</span>
              <span class="row-status" :class="`tone-${presentResult(record, record.status_code).tone}`">
                {{ record.ok ? record.status_code : presentResult(record).title }}
              </span>
              <span class="row-time">{{ formatDuration(record.elapsed_ms) }}</span>
              <span class="row-fill" />
              <span class="row-date">{{ formatDateTime(record.created_at) }}</span>
            </div>
            <div class="row-url mono truncate">{{ record.url }}</div>
          </div>
          <ChevronRight :size="13" class="row-chevron" />
        </button>
        <EmptyState
          v-if="!httpItems.length && !loading"
          :icon="History"
          title="没有 HTTP 请求记录"
          description="在「接口测试」页发送请求后，这里会保留脱敏后的请求与响应摘要。"
        />
      </div>

      <div v-else-if="tab === 'stream'" class="list scroll-y">
        <button v-for="record in streamItems" :key="record.id" class="row"
          :class="{ 'row-active': selectedStream?.id === record.id }" @click="selectedStream = record">
          <div class="row-main">
            <div class="row-top">
              <span class="tag tag-accent">{{ record.kind === 'sse' ? 'SSE' : 'WS' }}</span>
              <span class="row-status">{{ record.status }}</span>
              <span class="row-fill" />
              <span class="row-date">{{ formatDateTime(record.created_at) }}</span>
            </div>
            <div class="row-url mono truncate">{{ record.url }}</div>
          </div>
          <ChevronRight :size="13" class="row-chevron" />
        </button>
        <EmptyState v-if="!streamItems.length && !loading" :icon="Radio" title="没有长连接会话"
          description="在「长连接测试」页建立连接后，这里会显示会话结果。" />
      </div>
      <div v-else class="list scroll-y">
        <button v-for="item in taskItems" :key="item.id" class="row"
          :class="{ 'row-active': selectedTaskId === item.id }" @click="selectTask(item.id)">
          <div class="row-main">
            <div class="row-top">
              <span class="tag" :class="`tag-${taskProgress(item.status).tone}`">{{ taskProgress(item.status).label }}</span>
              <span class="row-fill" />
              <span class="row-date">{{ formatDateTime(item.created_at) }}</span>
            </div>
            <div class="row-url truncate" :title="taskHistoryLabel(item)">{{ taskHistoryLabel(item) }}</div>
          </div>
          <ChevronRight :size="13" class="row-chevron" />
        </button>
        <EmptyState v-if="!taskItems.length && !loading" :icon="History" title="没有智能群测记录"
          description="在「智能群测」页运行文档任务后，这里会显示逐条结果与总结。" />
      </div>
      </template>
      <template #second>
      <div v-if="tab === 'http'" class="detail scroll-y">
        <template v-if="selectedHttp">
          <div class="detail-head">
            <div class="detail-title-row">
              <span class="method" :class="`method-${selectedHttp.method.toLowerCase()}`">
                {{ selectedHttp.method }}
              </span>
              <span v-if="isSseHistory(selectedHttp)" class="tag tag-accent">SSE</span>
              <span class="tag" :class="`tag-${activeTone === 'neutral' ? '' : activeTone}`">
                {{ selectedHttp.ok ? selectedHttp.status_code : presentResult(selectedHttp).title }}
              </span>
              <span class="tag">{{ formatDuration(selectedHttp.elapsed_ms) }}</span>
              <span class="tag">{{ formatBytes(selectedHttp.response_bytes) }}</span>
              <div class="row-fill" />
              <CopyButton
                :text="String(selectedHttp.response_preview ?? '')"
                label="复制响应"
              />
              <button class="btn btn-ghost btn-sm" @click="removeRecord(selectedHttp)">
                <Trash2 :size="12" />
              </button>
            </div>
            <div class="detail-url mono">{{ selectedHttp.url }}</div>
            <div class="hint">{{ formatDateTime(selectedHttp.created_at) }}</div>
          </div>

          <div v-if="!selectedHttp.ok" class="detail-error">
            <strong>
              {{ presentResult(selectedHttp).title }}<template v-if="resultCodeLabel(presentResult(selectedHttp).resultStatus)"> · {{ resultCodeLabel(presentResult(selectedHttp).resultStatus) }}</template>
              <template v-if="selectedHttp.error_kind"> · <code>{{ selectedHttp.error_kind }}</code></template>
            </strong>
            <span>{{ presentResult(selectedHttp).message }}</span>
          </div>

          <div class="detail-section">
            <div class="mini-label">请求头（已脱敏）</div>
            <div v-for="(value, name) in selectedHttp.request_headers" :key="name" class="header-line">
              <span class="header-name mono">{{ name }}</span>
              <span class="header-value mono">{{ value }}</span>
            </div>
            <p v-if="!Object.keys(selectedHttp.request_headers).length" class="hint">未设置自定义头。</p>
          </div>

          <div v-if="selectedHttp.body_preview" class="detail-section">
            <div class="mini-label">请求体预览</div>
            <pre class="code">{{ prettyJson(selectedHttp.body_preview) }}</pre>
          </div>

          <div class="detail-section">
            <div class="mini-label">响应头</div>
            <div v-for="(value, name) in selectedHttp.response_headers" :key="name" class="header-line">
              <span class="header-name mono">{{ name }}</span>
              <span class="header-value mono">{{ JSON.stringify(value).replace(/[[\]"]/g, '') }}</span>
            </div>
            <p v-if="!Object.keys(selectedHttp.response_headers).length" class="hint">
              没有记录到响应头（请求未送达）。
            </p>
          </div>

          <div v-if="selectedHttp.response_preview" class="detail-section">
            <div class="mini-label">
              响应体预览
              <span class="hint">（最多保留前 8KiB；完整长度 {{ formatBytes(selectedHttp.response_bytes) }}）</span>
            </div>
            <pre class="code">{{ prettyJson(selectedHttp.response_preview) }}</pre>
          </div>
        </template>
        <EmptyState
          v-else
          :icon="Inbox"
          title="选择左侧一条记录"
          description="这里会显示脱敏后的请求头、请求体预览与响应内容。"
        />
      </div>
      <div v-else-if="tab === 'stream'" class="detail scroll-y">
        <div v-if="selectedStream" class="detail-head">
          <div class="detail-title-row">
            <span class="tag tag-accent">{{ selectedStream.kind === 'sse' ? 'SSE' : 'WS' }}</span>
            <span class="tag" :class="`tag-${selectedStream.status === 'closed' ? 'success' : selectedStream.status === 'error' ? 'danger' : 'neutral'}`">{{ selectedStream.status }}</span>
            <span class="tag">{{ selectedStream.event_count }} 事件</span>
            <span class="tag">{{ formatBytes(selectedStream.byte_count) }}</span>
          </div>
          <div class="detail-url mono">{{ selectedStream.url }}</div>
          <div class="hint">{{ formatDateTime(selectedStream.opened_at ?? selectedStream.created_at) }} · {{ describeStopReason(selectedStream.stop_reason) }}</div>
          <p v-if="selectedStream.error_message" class="detail-error">{{ selectedStream.error_message }}</p>
        </div>
        <EmptyState v-else :icon="Inbox" title="选择左侧一条会话" />
      </div>
      <div v-else class="detail scroll-y">
        <template v-if="task">
          <div class="detail-head">
            <strong class="detail-url">{{ taskHistoryLabel(task) }}</strong>
            <div class="detail-title-row">
              <span class="tag" :class="`tag-${taskProgress(task.status).tone}`">{{ taskProgress(task.status).label }}</span>
              <span class="hint">{{ task.results.length }} / {{ task.snapshot.cases.length }} 条已记录</span>
              <a class="btn btn-sm" :href="`/api/tasks/${encodeURIComponent(task.id)}/report`" download>导出总结</a>
            </div>
            <div class="detail-title-row">
              <span v-if="streamWait" class="tag tag-info">{{ streamWait.label }} 1</span>
              <span v-for="item in taskOutcomes" :key="item.status" class="tag" :class="`tag-${item.tone}`">{{ item.label }} {{ item.count }}</span>
            </div>
          </div>
          <p v-if="task.error_kind" class="detail-error">{{ task.error_kind }}</p>
          <TaskResultTree :key="task.id" :task="task" :stream-wait="streamWait" />
          <div class="detail-section"><div class="mini-label">AI 总结</div><pre class="code">{{ task.summary || '暂无总结。' }}</pre></div>
        </template>
        <p v-else-if="taskLoading" class="hint" role="status">正在读取智能群测结果…</p>
        <EmptyState v-else :icon="Inbox" title="选择左侧一条用例任务" />
      </div>
      </template>
    </ResizableSplit>
  </div>
</template>

<style scoped>
.history-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 16px 10px;
  flex: none;
}

.page-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.015em;
}

.page-actions {
  display: flex;
  gap: 7px;
  flex: none;
}

.tabs {
  display: flex;
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
}

.tab-active {
  color: var(--accent);
  font-weight: 600;
}

.results-split { flex: 1; }

.list {
  flex: 1;
  min-height: 0;
  padding: 10px 12px 16px;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
}

.row:hover {
  background: var(--bg-hover);
}

.row-active {
  background: var(--accent-soft);
  border-color: var(--accent-border);
}

.row-main {
  flex: 1;
  min-width: 0;
}

.row-top {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

.row-status {
  font-family: var(--font-mono);
  font-weight: 600;
}

.tone-success { color: var(--success); }
.tone-info { color: var(--info); }
.tone-warning { color: var(--warning); }
.tone-danger { color: var(--danger); }
.tone-neutral { color: var(--text-tertiary); }

.row-time,
.row-date {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.row-fill {
  flex: 1;
}

.row-url {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-secondary);
}

.row-chevron {
  color: var(--text-tertiary);
  flex: none;
}

.detail {
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 12px 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-head {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.detail-title-row {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.detail-url {
  font-size: 12px;
  color: var(--text-secondary);
  word-break: break-all;
}

.detail-error {
  display: flex;
  gap: 8px;
  padding: 9px 11px;
  border-radius: var(--radius-md);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 11.5px;
}

.detail-section {
  border-top: 1px solid var(--border-subtle);
  padding-top: 10px;
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
  gap: 10px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border-subtle);
  font-size: 11px;
}

.header-name {
  flex: none;
  min-width: 130px;
  font-weight: 600;
  color: var(--text-secondary);
}

.header-value {
  color: var(--text-tertiary);
  word-break: break-all;
}

.code {
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}

.results-split.split-horizontal .list { border-right: 1px solid var(--border-subtle); }
.results-split.split-vertical .list { border-bottom: 1px solid var(--border-subtle); }
</style>
