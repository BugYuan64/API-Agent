<script setup lang="ts">
/** WebSocket 调试页面：依赖 streams API、请求头行模型与共享布局，启动会话、增量读取事件并回放历史。 */

import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Cable,
  CircleStop,
  Loader2,
  Play,
  Radio,
  Send,
  ServerOff,
} from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import ResizableSplit from '@/components/ResizableSplit.vue'
import HistoryList, { type HistoryEntry } from '@/components/HistoryList.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import { useHeaderRows } from '@/composables/useHeaderRows'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import {
  deleteStream,
  listStreams,
  readStream,
  sendStreamMessage,
  startStream,
  stopStream,
} from '@/api/endpoints'
import type { StreamEvent, StreamSession } from '@/api/types'
import {
  describeStopReason,
  formatBytes,
  formatDuration,
  formatRelative,
  looksLikeJson,
  prettyJson,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { presentResult, resultCodeLabel } from '@/lib/results'

/** 长连接表单仅配置 WebSocket 握手、消息和会话上限。 */
const form = reactive({
  url: 'ws://127.0.0.1:8010/ws',
  subprotocols: '',
  maxEvents: 200,
  maxDurationMs: 60_000,
  connectTimeoutMs: 10_000,
  pingIntervalMs: 20_000,
  scheduled: '',
  sendIntervalMs: 500,
})

/** 连接配置区宽度：内层分栏（配置|时间线）的受控尺寸。 */
const configWidth = ref(320)
try {
  const saved = Number(window.localStorage.getItem('api-agent.layout.long-connection.config'))
  if (Number.isFinite(saved) && saved > 0) configWidth.value = saved
} catch {
  // 存储不可用时保持默认宽度。
}

const session = ref<StreamSession | null>(null)
const events = ref<StreamEvent[]>([])
const cursor = ref(0)
const connecting = ref(false)
const stopping = ref(false)
const outgoing = ref('')
const sendingMessage = ref(false)
const history = ref<{ id: string; kind: string; url: string; status: string; stop_reason: string | null; event_count: number; created_at: string }[]>([])
const activeHistoryId = ref<string | null>(null)
const filter = ref<'all' | 'in' | 'out'>('all')
const autoScroll = ref(true)
const logRef = ref<HTMLElement | null>(null)

// 一个可取消的长轮询循环：同一个 Session 只允许存在一条轮询链。
let pollAbort: AbortController | null = null
let pollTimer: number | undefined

/** 按会话是否存在及 finished 判断仍可操作的会话；不以事件数量推断连接状态。 */
const isLive = computed(
  () => !!session.value && !session.value.finished,
)
/** 按收发方向过滤当前事件用于展示；全部模式返回完整列表，不移动服务端游标。 */
const filteredEvents = computed(() =>
  filter.value === 'all' ? events.value : events.value.filter((item) => item.direction === filter.value),
)

/** 根据当前会话及 finished 生成展示色调；终止错误为 danger，其余结束状态为 neutral。 */
const statusTone = computed(() => {
  if (!session.value) return 'neutral'
  if (session.value.finished) {
    return session.value.status === 'error' ? 'danger' : 'neutral'
  }
  return 'success'
})

/** 将当前会话状态转换为中文标签；无会话显示未连接，未知状态保留原值。 */
const statusLabel = computed(() => {
  if (!session.value) return '未连接'
  const labels: Record<string, string> = {
    connecting: '握手中',
    open: '已连接',
    closed: '已关闭',
    error: '出错',
    cancelled: '已停止',
  }
  return labels[session.value.status] ?? session.value.status
})

/** 新事件到达且 autoScroll 开关开启时，等待 DOM 更新后滚动到底部；不检测用户滚动位置。 */
watch(
  () => events.value.length,
  async () => {
    if (!autoScroll.value) return
    await nextTick()
    const element = logRef.value
    if (element) element.scrollTop = element.scrollHeight
  },
)

/** 头行编辑与折叠规则与接口调试页共用同一份实现。 */
const {
  rows: headerRows,
  headers: activeHeaders,
} = useHeaderRows()

/** 把多行文本框拆成待发消息列表：空行忽略，但保留行内空格。 */
const scheduledMessages = computed(() =>
  form.scheduled.split('\n').filter((line) => line.trim().length > 0),
)

/** 提交当前协议配置并重置本页事件游标，成功后启动管理接口长轮询与刷新历史；空地址拦截，失败提示并解除 connecting。 */
async function start() {
  if (!form.url.trim()) {
    useToastStore().push('warning', '请先填写长连接地址')
    return
  }
  stopPolling()
  connecting.value = true
  events.value = []
  cursor.value = 0
  activeHistoryId.value = null
  try {
    const created = await startStream({
      kind: 'websocket',
      url: form.url.trim(),
      headers: activeHeaders.value,
      subprotocols: form.subprotocols
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
      max_events: form.maxEvents,
      max_duration_ms: form.maxDurationMs,
      connect_timeout_ms: form.connectTimeoutMs,
      ping_interval_ms: form.pingIntervalMs,
      send: scheduledMessages.value,
      send_interval_ms: form.sendIntervalMs,
      project_id: null,
    })
    session.value = created
    startPolling(created.id)
    await loadHistory()
  } catch (error) {
    useToastStore().push('danger', '长连接未启动', describeError(error))
  } finally {
    connecting.value = false
  }
}

/** 轮询循环：用 wait_ms 长轮询，服务端有事件就立刻返回。
 *  这样界面接近实时，又不必维持第二条浏览器长连接。 */
function startPolling(sessionId: string) {
  /** 读取指定会话游标后的事件并追加到时间线；会话终止时刷新历史，中止或读取失败退出，否则延迟继续读取。 */
  const step = async () => {
    if (!session.value || session.value.id !== sessionId || session.value.finished) return
    pollAbort = new AbortController()
    try {
      const page = await readStream(sessionId, cursor.value, 1200, pollAbort.signal)
      session.value = page.session
      if (page.events.length) {
        events.value.push(...page.events)
        cursor.value = page.next_cursor
      }
      if (page.finished) {
        await loadHistory()
        return
      }
    } catch (error) {
      // 主动中止（停止/重连）不算错误，静默退出循环。
      if (pollAbort?.signal.aborted) return
      useToastStore().push('warning', '读取会话失败', describeError(error))
      return
    }
    pollTimer = window.setTimeout(step, 60)
  }
  void step()
}

/** 中止当前读取并清理下次调度；只停止浏览器观测，不结束后端会话。 */
function stopPolling() {
  pollAbort?.abort()
  pollAbort = null
  window.clearTimeout(pollTimer)
}

/** 请求服务端结束当前会话，成功后停轮询、刷新历史并展示结束原因；失败提示并始终解除 stopping。 */
async function stop() {
  if (!session.value) return
  stopping.value = true
  try {
    const stopped = await stopStream(session.value.id)
    session.value = stopped
    stopPolling()
    await loadHistory()
    useToastStore().push('info', '已停止会话', `结束原因：${describeStopReason(stopped.stop_reason)}`)
  } catch (error) {
    useToastStore().push('danger', '停止失败', describeError(error))
  } finally {
    stopping.value = false
  }
}

/** 将非空输入作为消息发送到当前会话并更新摘要；成功后清空输入，失败保留输入并提示，始终解除发送态。 */
async function sendMessage() {
  if (!session.value || !outgoing.value.trim()) return
  sendingMessage.value = true
  try {
    session.value = await sendStreamMessage(session.value.id, outgoing.value)
    outgoing.value = ''
  } catch (error) {
    useToastStore().push('danger', '发送失败', describeError(error))
  } finally {
    sendingMessage.value = false
  }
}

/** 回放指定历史的事件并更新会话摘要；会话结束与事件读尽独立判断，最多读取 40 页或遇空页停止，失败提示并保留已读事件。 */
async function replay(record: (typeof history.value)[number]) {
  stopPolling()
  activeHistoryId.value = record.id
  events.value = []
  cursor.value = 0
  try {
    let hasMore = true
    let guard = 0
    while (hasMore && guard < 40) {
      const page = await readStream(record.id, cursor.value, 0)
      session.value = page.session
      if (page.events.length) {
        events.value.push(...page.events)
        cursor.value = page.next_cursor
      }
      hasMore = page.has_more
      guard += 1
      if (!page.events.length) break
    }
    useToastStore().push('info', '已回放历史会话', '数据来自数据库，含当时落库的全部事件')
  } catch (error) {
    useToastStore().push('danger', '回放失败', describeError(error))
  }
}

/** 读取最近长连接会话供回放；管理请求失败时回退空列表，不影响当前连接。 */
async function loadHistory() {
  try {
    const page = await listStreams(20)
    history.value = page.items.filter(item => item.kind === 'websocket')
  } catch {
    history.value = []
  }
}

/**
 * 删除一条会话记录。
 *
 * 作用：删除的是历史回看线索；若该会话正在跑，后端会先收尾再删，
 * 因此本页必须同时把当前连接状态复位，否则界面还显示一个已不存在的连接。
 * 实现：成功后从本地列表移除；若删的正是当前选中项，清空选中并断开显示。
 */
async function removeHistory(record: (typeof history.value)[number]) {
  try {
    await deleteStream(record.id)
    history.value = history.value.filter((item) => item.id !== record.id)
    if (activeHistoryId.value === record.id) {
      activeHistoryId.value = null
      session.value = null
      events.value = []
    }
    useToastStore().push('success', '会话记录已删除')
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 按共享历史列表提供的 ID 发起删除；记录未加载时忽略。 */
function removeHistoryById(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) removeHistory(target)
}

/** 按事件方向和种类返回中文标签；发送方向优先，未知事件种类保留原值。 */
function eventLabel(event: StreamEvent): string {
  if (event.direction === 'out') return '发送'
  const labels: Record<string, string> = {
    open: '已连接',
    close: '已关闭',
    comment: '注释',
    message: event.event_name ? `事件 ${event.event_name}` : '消息',
    text: '文本帧',
    binary: '二进制帧',
    error: '错误',
    raw: '原始行',
  }
  return labels[event.kind] ?? event.kind
}

/** 生成事件正文展示：二进制显示截断的 base64 预览，合法 JSON 美化，其余原样保留。 */
function eventBody(event: StreamEvent): string {
  const data = event.data ?? ''
  if (event.kind === 'binary') return `base64: ${data.slice(0, 600)}`
  return looksLikeJson(data) ? prettyJson(data) : data
}

/** 把会话记录映射成共享历史列的 WebSocket 条目。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  history.value.map((record) => ({
    id: record.id,
    label: `WS · ${record.url}`,
    status: describeStopReason(record.stop_reason),
    tone: record.status === 'closed' ? 'success' : record.status === 'error' ? 'danger' : 'neutral',
    meta: `${record.event_count} 条 · ${formatRelative(record.created_at)}`,
  })),
)

/** 共享历史列只回传 id。 */
function replayById(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) replay(target)
}

onMounted(loadHistory)
onBeforeUnmount(stopPolling)
</script>

<template>
  <TestWorkspace persist-key="api-agent.layout.long-connection.history" class="lc">
    <!-- 左：最近会话（独立列，可拖动宽度） -->
    <template #history>
      <HistoryList
        title="最近会话"
        :items="historyEntries"
        :active-id="activeHistoryId"
        empty-text="还没有会话记录。建立一次连接后会出现在这里。"
        :on-delete="removeHistoryById"
        @select="replayById"
      />
    </template>

    <!-- 右：配置 + 时间线 -->
    <div class="lc-main">
      <ResizableSplit
        v-model:size="configWidth"
        persist-key="api-agent.layout.long-connection.config"
        :min="280"
        :min-other="360"
        label="拖动调整连接配置区与时间线区宽度"
      >
        <template #first>
      <aside class="lc-config scroll-y">
      <div class="config-head">
        <h2 class="config-title">WebSocket 调试</h2>
        <p class="config-sub">WebSocket 双向连接调试</p>
      </div>

      <label class="field">
        <span class="field-label">地址</span>
        <input
          v-model="form.url"
          class="input mono"
          spellcheck="false"
          placeholder="ws://127.0.0.1:9001/ws"
          @keydown.enter.exact.prevent="start"
        />
        <span class="hint">
          ws:// 或 wss:// WebSocket 地址
        </span>
      </label>

      <div class="field">
        <span class="field-label">请求头</span>
        <div class="header-list">
          <div v-for="(row, index) in headerRows" :key="index" class="header-row">
            <input v-model="row.name" class="input mono" placeholder="名称" spellcheck="false" />
            <input v-model="row.value" class="input mono" placeholder="值" spellcheck="false" />
            <button
              class="btn btn-ghost btn-icon btn-sm"
              title="删除"
              @click="headerRows.splice(index, 1)"
            >
              ×
            </button>
          </div>
        </div>
        <button class="btn btn-sm" @click="headerRows.push({ name: '', value: '' })">
          + 添加
        </button>
      </div>

      <label class="field">
        <span class="field-label">子协议（逗号分隔）</span>
        <input v-model="form.subprotocols" class="input mono" placeholder="graphql-ws" />
      </label>

      <label class="field">
        <span class="field-label">连接后自动发送（每行一条）</span>
        <textarea
          v-model="form.scheduled"
          class="textarea mono"
          rows="3"
          spellcheck="false"
          placeholder='{"type":"subscribe"}'
        />
      </label>

      <label class="field">
        <span class="field-label">发送间隔（毫秒）</span>
        <input v-model.number="form.sendIntervalMs" class="input" type="number" min="0" max="60000" step="50" />
      </label>

      <div class="grid-2">
        <label class="field">
          <span class="field-label">最多事件</span>
          <input v-model.number="form.maxEvents" class="input" type="number" min="1" max="2000" />
        </label>
        <label class="field">
          <span class="field-label">最长时长（ms）</span>
          <input v-model.number="form.maxDurationMs" class="input" type="number" min="1000" max="900000" step="1000" />
        </label>
      </div>

      <div class="grid-2">
        <label class="field">
          <span class="field-label">连接超时（ms）</span>
          <input v-model.number="form.connectTimeoutMs" class="input" type="number" min="100" max="60000" step="100" />
        </label>
        <label class="field">
          <span class="field-label">心跳间隔（ms）</span>
          <input v-model.number="form.pingIntervalMs" class="input" type="number" min="0" max="300000" step="1000" />
        </label>
      </div>

      <div class="actions">
        <button v-if="!isLive" class="btn btn-accent start-button" :disabled="connecting" @click="start">
          <Loader2 v-if="connecting" :size="14" class="spinning" />
          <Play v-else :size="14" />
          {{ connecting ? '连接中' : '开始连接' }}
        </button>
        <button v-else class="btn btn-danger start-button" :disabled="stopping" @click="stop">
          <CircleStop :size="14" />
          {{ stopping ? '停止中' : '停止会话' }}
        </button>
      </div>

      <div class="scope-note">
        <ServerOff :size="13" />
        <p>
          只允许本机回环地址。<strong>上限</strong>是硬约束：达到事件数、字节数或时长后由本服务主动断开，
          结束原因会记为 <code>limits_exceeded</code> / <code>deadline_exceeded</code>，与目标主动关闭区分开。
        </p>
      </div>

    </aside>
        </template>
        <template #second>
    <!-- 右：会话与时间线 -->
    <section class="lc-stage">
      <div class="stage-head">
        <div class="stage-title">
          <span class="stage-dot" :class="`dot-${statusTone}`" />
          <span>{{ statusLabel }}</span>
          <span v-if="session" class="tag">WebSocket</span>
          <span v-if="session?.subprotocol" class="tag tag-info">子协议 {{ session.subprotocol }}</span>
        </div>
        <div class="stage-stats" v-if="session">
          <span class="stat"><ArrowDownToLine :size="12" />{{ session.event_count }} 事件</span>
          <span class="stat"><ArrowUpFromLine :size="12" />{{ session.sent_count }} 发送</span>
          <span class="stat">{{ formatBytes(session.byte_count) }}</span>
          <span v-if="session.truncated_count" class="stat stat-warn">
            {{ session.truncated_count }} 条截断
          </span>
          <span class="stat">{{ describeStopReason(session.stop_reason) }}</span>
          <CopyButton v-if="session" :text="session.url" label="复制地址" />
        </div>
      </div>

      <div v-if="session?.error_kind" class="stage-error">
        <strong>{{ presentResult(session).title }}<template v-if="resultCodeLabel(presentResult(session).resultStatus)"> · {{ resultCodeLabel(presentResult(session).resultStatus) }}</template></strong>
        <span>{{ presentResult(session).message }}</span>
      </div>

      <div v-if="session && !session.finished" class="send-bar">
        <input
          v-model="outgoing"
          class="input mono"
          placeholder="向目标发送一条文本消息…"
          spellcheck="false"
          @keydown.enter.exact.prevent="sendMessage"
        />
        <button class="btn" :disabled="sendingMessage || !outgoing.trim()" @click="sendMessage">
          <Send :size="13" />
          发送
        </button>
      </div>

      <div class="timeline-toolbar">
        <div class="filter-group">
          <button
            v-for="item in [
              { key: 'all', label: '全部' },
              { key: 'in', label: '接收' },
              { key: 'out', label: '发送' },
            ]"
            :key="item.key"
            class="filter-chip"
            :class="{ 'filter-chip-active': filter === item.key }"
            @click="filter = item.key as typeof filter"
          >
            {{ item.label }}
          </button>
        </div>
        <div class="toolbar-fill" />
        <label class="auto-scroll">
          <input v-model="autoScroll" type="checkbox" class="switch" />
          自动滚动
        </label>
        <CopyButton
          :text="events.map((event) => `[${event.sequence}] ${event.direction} ${eventLabel(event)}: ${event.data ?? ''}`).join('\n')"
          label="导出时间线"
        />
      </div>

      <div ref="logRef" class="timeline scroll-y">
        <div v-if="!session" class="timeline-empty">
          <EmptyState
            :icon="Cable"
            title="开始 WebSocket 调试"
            description="记录每一帧、关闭码与协商到的子协议。收尾原因会区分目标主动关闭和本服务按上限停止。"
          />
        </div>
        <div v-else-if="!filteredEvents.length" class="timeline-empty">
          <EmptyState
            :icon="Radio"
            :title="isLive ? '等待目标推送…' : '没有符合筛选的事件'"
            :description="isLive ? '连接已建立，目标还没有发来数据。' : '可以切换筛选条件，或检查结束原因。'"
          />
        </div>
        <article
          v-for="event in filteredEvents"
          :key="event.sequence"
          class="event"
          :class="[`event-${event.direction}`, { 'event-lifecycle': ['open', 'close', 'error', 'comment'].includes(event.kind) }]"
        >
          <header class="event-head">
            <span class="event-seq">#{{ event.sequence }}</span>
            <span class="event-dir" :class="`dir-${event.direction}`">
              <ArrowDownToLine v-if="event.direction === 'in'" :size="11" />
              <ArrowUpFromLine v-else :size="11" />
              {{ event.direction === 'in' ? '收' : '发' }}
            </span>
            <span class="event-kind">{{ eventLabel(event) }}</span>
            <span class="event-fill" />
            <span class="event-size">{{ formatBytes(event.data_bytes) }}</span>
            <span class="event-time">+{{ formatDuration(event.elapsed_ms) }}</span>
          </header>
          <pre v-if="event.data" class="event-body">{{ eventBody(event) }}</pre>
          <div v-if="event.truncated" class="event-truncated">该条事件已截断（保留前 64KiB）</div>
        </article>
      </div>

      <footer v-if="session" class="stage-foot">
        <span>会话 {{ session.id.slice(0, 8) }}</span>
        <span v-if="session.http_status">HTTP {{ session.http_status }}</span>
        <span v-if="session.content_type" class="truncate">{{ session.content_type }}</span>
        <span v-if="session.close_code != null">关闭码 {{ session.close_code }}</span>
        <span v-if="session.opened_at">建立于 {{ formatRelative(session.opened_at) }}</span>
        <span class="foot-fill" />
        <span>{{ cursor }} / {{ session.event_count }} 已加载</span>
      </footer>
      </section>
        </template>
      </ResizableSplit>
    </div>
  </TestWorkspace>
</template>

<style scoped>
.lc {
  height: 100%;
  min-height: 0;
}

/* 最近会话列：填满左侧插槽，自身滚动 */
/* 右半：配置栏与时间线并排 */
.lc-main {
  display: flex;
  height: 100%;
  min-height: 0;
}

/* 内层分栏是 flex 子项：必须显式撑满，否则会按内容宽度塌缩，
   导致可用宽度测错、拖动上限被压到几百像素。 */
.lc-main > .resizable-split {
  flex: 1;
  min-width: 0;
}

/* ---------- 配置栏 ---------- */
.lc-config {
  height: 100%;
  padding: 16px 16px 24px;
  border-right: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.config-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.config-title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.config-sub {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
}

.header-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 5px;
}

.header-row {
  display: grid;
  grid-template-columns: 1fr 1.2fr 26px;
  gap: 5px;
}

.header-row .input {
  height: 29px;
}

.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 9px;
}

.actions {
  display: flex;
  gap: 7px;
  margin-top: 2px;
}

.start-button {
  flex: 1;
  height: 34px;
}

.scope-note {
  display: flex;
  gap: 8px;
  padding: 10px 11px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  color: var(--text-tertiary);
}

.scope-note p {
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
}

.scope-note strong {
  color: var(--text-secondary);
}


/* ---------- 舞台 ---------- */
.lc-stage {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
  /* 与其他测试页保持一致的主题表面。 */
  background: var(--bg-surface);
}

.stage-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 16px;
  background: var(--panel-header-gradient);
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
  flex-wrap: wrap;
}

.stage-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 600;
}

.stage-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.dot-success {
  background: var(--success);
  box-shadow: 0 0 0 3px var(--success-soft);
  animation: pulse 1.8s ease-in-out infinite;
}

.dot-danger {
  background: var(--danger);
}

.dot-neutral {
  background: var(--border-strong);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}

.stage-stats {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.stat {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.stat-warn {
  color: var(--warning);
}

.stage-error {
  display: flex;
  gap: 8px;
  padding: 8px 16px;
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 11.5px;
  flex: none;
}

.stage-error strong {
  font-family: var(--font-mono);
}

.send-bar {
  display: flex;
  gap: 7px;
  padding: 9px 16px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.send-bar .btn {
  height: 32px;
}

.timeline-toolbar {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 16px;
  flex: none;
}

.filter-group {
  display: flex;
  gap: 2px;
  padding: 2px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
}

.filter-chip {
  height: 22px;
  padding: 0 9px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11.5px;
}

.filter-chip-active {
  background: var(--control-gradient);
  color: var(--text-primary);
  font-weight: 600;
  box-shadow: var(--shadow-sm);
}

.toolbar-fill {
  flex: 1;
}

.auto-scroll {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--text-secondary);
}

.switch {
  width: 14px;
  height: 14px;
  accent-color: var(--accent);
}

.timeline {
  flex: 1;
  min-height: 0;
  padding: 0 16px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.timeline-empty {
  display: grid;
  place-items: center;
  flex: 1;
}

.event {
  border: 1px solid var(--border-subtle);
  border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-gradient);
  overflow: hidden;
}

.event-out {
  border-left-color: var(--accent);
}

.event-lifecycle {
  background: var(--bg-subtle);
  border-left-color: var(--text-tertiary);
}

.event-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 9px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.event-seq {
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}

.event-dir {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-weight: 600;
}

.dir-in { color: var(--success); }
.dir-out { color: var(--accent); }

.event-kind {
  color: var(--text-secondary);
  font-weight: 500;
}

.event-fill {
  flex: 1;
}

.event-size,
.event-time {
  font-family: var(--font-mono);
}

.event-body {
  margin: 0;
  padding: 0 9px 8px;
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-primary);
}

.event-truncated {
  padding: 4px 9px;
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 10.5px;
}

.stage-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: none;
  height: 30px;
  padding: 0 16px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  font-size: 10.5px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.foot-fill {
  flex: 1;
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1120px) {
  .lc {
    flex-direction: column;
  }

  .lc-config {
    width: 100%;
    max-height: 46%;
    border-right: 0;
    border-bottom: 1px solid var(--border-subtle);
  }
}
</style>
