<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChevronRight, History, Inbox, Radio, Trash2 } from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import {
  clearConsoleHistory,
  deleteConsoleHistory,
  listConsoleHistory,
  listExecutions,
  listStreams,
} from '@/api/endpoints'
import type { ConsoleHistory, Execution, StreamHistory } from '@/api/types'
import {
  describeExecutionStatus,
  describeStopReason,
  formatBytes,
  formatDateTime,
  formatDuration,
  prettyJson,
  statusTone,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

/** 请求历史：把三类证据（HTTP 请求、长连接会话、计划执行）放在同一页，
 *  因为它们回答的是同一个问题——“我做过什么，结果如何”。 */
const tab = ref<'http' | 'stream' | 'execution'>('http')
const httpItems = ref<ConsoleHistory[]>([])
const streamItems = ref<StreamHistory[]>([])
const executionItems = ref<Execution[]>([])
const loading = ref(false)
const selectedHttp = ref<ConsoleHistory | null>(null)

const activeTone = computed(() => (selectedHttp.value ? statusTone(selectedHttp.value.status_code) : 'neutral'))

async function load() {
  loading.value = true
  try {
    const [http, streams, executions] = await Promise.allSettled([
      listConsoleHistory(60),
      listStreams(60),
      listExecutions(60),
    ])
    httpItems.value = http.status === 'fulfilled' ? http.value.items : []
    streamItems.value = streams.status === 'fulfilled' ? streams.value.items : []
    executionItems.value = executions.status === 'fulfilled' ? executions.value.items : []
    const failure = [http, streams, executions].find((item) => item.status === 'rejected')
    if (failure && failure.status === 'rejected') {
      useToastStore().push('warning', '部分历史加载失败', describeError(failure.reason))
    }
  } finally {
    loading.value = false
  }
}

async function removeRecord(record: ConsoleHistory) {
  try {
    await deleteConsoleHistory(record.id)
    httpItems.value = httpItems.value.filter((item) => item.id !== record.id)
    if (selectedHttp.value?.id === record.id) selectedHttp.value = null
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

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

onMounted(load)
</script>

<template>
  <div class="history-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">请求历史</h2>
        <p class="page-sub">
          HTTP 请求与长连接会话都存本机 SQLite；凭据类头在落库前已脱敏。计划执行结果在「执行结果」页。
        </p>
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
          { key: 'execution', label: `计划执行 ${executionItems.length}` },
        ]"
        :key="item.key"
        class="tab"
        :class="{ 'tab-active': tab === item.key }"
        @click="tab = item.key as typeof tab"
      >
        {{ item.label }}
      </button>
    </div>

    <!-- HTTP 请求 -->
    <div v-if="tab === 'http'" class="split">
      <div class="list scroll-y">
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
              <span class="row-status" :class="`tone-${statusTone(record.status_code)}`">
                {{ record.ok ? record.status_code : record.error_kind }}
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
          description="在「接口调试」页发送一次请求，这里会保留脱敏后的请求与响应摘要。"
        />
      </div>

      <div class="detail scroll-y">
        <template v-if="selectedHttp">
          <div class="detail-head">
            <div class="detail-title-row">
              <span class="method" :class="`method-${selectedHttp.method.toLowerCase()}`">
                {{ selectedHttp.method }}
              </span>
              <span class="tag" :class="`tag-${activeTone === 'neutral' ? '' : activeTone}`">
                {{ selectedHttp.ok ? selectedHttp.status_code : selectedHttp.error_kind }}
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

          <div v-if="selectedHttp.error_message" class="detail-error">
            <strong>{{ selectedHttp.error_kind }}</strong>
            <span>{{ selectedHttp.error_message }}</span>
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
    </div>

    <!-- 长连接会话 -->
    <div v-else-if="tab === 'stream'" class="list scroll-y wide">
      <table v-if="streamItems.length" class="table">
        <thead>
          <tr>
            <th>协议</th>
            <th>地址</th>
            <th>状态</th>
            <th>结束原因</th>
            <th>事件</th>
            <th>数据量</th>
            <th>建立时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="record in streamItems" :key="record.id">
            <td><span class="tag tag-accent">{{ record.kind === 'sse' ? 'SSE' : 'WS' }}</span></td>
            <td class="mono truncate url-cell">{{ record.url }}</td>
            <td>
              <span class="tag" :class="`tag-${record.status === 'closed' ? 'success' : record.status === 'error' ? 'danger' : ''}`">
                {{ record.status }}
              </span>
            </td>
            <td class="hint">{{ describeStopReason(record.stop_reason) }}</td>
            <td class="mono">{{ record.event_count }}</td>
            <td class="mono hint">{{ formatBytes(record.byte_count) }}</td>
            <td class="hint">{{ formatDateTime(record.opened_at ?? record.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <EmptyState
        v-else
        :icon="Radio"
        title="没有长连接会话"
        description="在「长连接测试」页建立一次 SSE 或 WebSocket 连接，这里会保留完整时间线。"
      />
    </div>

    <!-- 计划执行 -->
    <div v-else class="list scroll-y wide">
      <table v-if="executionItems.length" class="table">
        <thead>
          <tr>
            <th>标签</th>
            <th>结果</th>
            <th>请求</th>
            <th>耗时</th>
            <th>错误类别</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in executionItems" :key="item.id">
            <td>{{ item.label || '(未命名)' }}</td>
            <td>
              <span class="tag" :class="`tag-${item.status === 'passed' ? 'success' : item.status === 'failed' ? 'warning' : 'danger'}`">
                {{ describeExecutionStatus(item.status) }}
              </span>
            </td>
            <td class="mono">{{ item.observed_attempts }}/{{ item.reserved_requests }}</td>
            <td class="mono hint">{{ formatDuration(item.duration_ms) }}</td>
            <td class="mono hint">{{ item.error_kind ?? '—' }}</td>
            <td class="hint">{{ formatDateTime(item.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <EmptyState
        v-else
        :icon="Inbox"
        title="没有执行记录"
        description="在「用例计划」页执行一份计划，这里会保留每条断言的通过情况。"
      />
    </div>
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

.page-sub {
  margin: 3px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  max-width: 82ch;
  line-height: 1.7;
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
  border-bottom-color: var(--accent);
  font-weight: 600;
}

.split {
  display: flex;
  flex: 1;
  min-height: 0;
}

.list {
  flex: 1;
  min-height: 0;
  padding: 10px 12px 16px;
}

.list.wide {
  overflow-x: auto;
}

.split .list {
  flex: none;
  width: 44%;
  border-right: 1px solid var(--border-subtle);
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

.url-cell {
  max-width: 340px;
}

@media (max-width: 1080px) {
  .split {
    flex-direction: column;
  }

  .split .list {
    width: 100%;
    border-right: 0;
    border-bottom: 1px solid var(--border-subtle);
    max-height: 40%;
  }
}
</style>
