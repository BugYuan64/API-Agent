<script setup lang="ts">
/** Document-driven automatic testing, with persistent history and cancellable progress polling. */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import HistoryList, { type HistoryEntry } from '@/components/HistoryList.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import ResizableSplit from '@/components/ResizableSplit.vue'
import { describeError } from '@/api/client'
import { cancelTask, deleteTask, listTasks, readTask, recognizeDocument, startTask, type Catalog, type TaskRecord } from '@/api/tasks'
import { useToastStore } from '@/stores/toasts'

const catalog = ref<Catalog | null>(null)
const filename = ref('')
const origin = ref('')
const allowWrites = ref(false)
const observationMs = ref(1000)
const wallTime = ref(300)
const busy = ref(false)
const run = ref<TaskRecord | null>(null)
const history = ref<TaskRecord[]>([])
const width = ref(440)
const toast = useToastStore()
const running = computed(() => !!run.value && ['queued', 'running'].includes(run.value.status))
const total = computed(() => catalog.value ? catalog.value.cases.length + catalog.value.endpoints.reduce((sum, endpoint) => sum + 1 + (endpoint.cases?.length ?? 0) + (endpoint.generated_cases?.length ?? 0), 0) : 0)
const labels: Record<string, string> = { queued: '排队中', running: '执行中', completed: '流程完成', partial: '部分完成', cancelled: '已取消', timed_out: '已超时', interrupted: '已中断', passed: '通过', failed: '未通过', error: '错误', skipped: '未授权，已跳过', unknown: '未评估' }
const entries = computed<HistoryEntry[]>(() => history.value.map(item => ({ id: item.id, label: '文档任务 · ' + item.id.slice(0, 8), status: labels[item.status] ?? item.status, tone: 'neutral', meta: new Date(item.created_at).toLocaleString() })))
let generation = 0
let timer: number | undefined
let abort: AbortController | undefined
let disposed = false

/** Invalidate in-flight observations before switching records or leaving this page. */
function stopPolling() { generation++; window.clearTimeout(timer); abort?.abort() }
/** Refresh persistent history without replacing the currently selected result. */
async function refreshHistory() {
  const page = await listTasks()
  if (!disposed) history.value = page.items
}
/** Load a task using generation checks so stale responses cannot overwrite a newer selection. */
async function openHistory(id: string) {
  stopPolling()
  const current = generation
  abort = new AbortController()
  try {
    const record = await readTask(id, abort.signal)
    if (disposed || current !== generation) return
    run.value = record
    if (['queued', 'running'].includes(record.status)) timer = window.setTimeout(() => void openHistory(id), 800)
    else await refreshHistory()
  } catch (error) {
    if (current === generation && !disposed) toast.push('danger', '读取任务失败', describeError(error))
  }
}
/** Read one bounded UTF-8 file and automatically recognize all documented operations. */
async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  catalog.value = null
  filename.value = file.name
  busy.value = true
  try {
    if (file.size > 524288) throw new Error('文档最大 512 KiB')
    const text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
    const recognized = await recognizeDocument(file.name, text)
    if (!disposed) catalog.value = recognized
  } catch (error) { toast.push('danger', '识别失败', describeError(error)) }
  finally { busy.value = false }
}
/** Submit the recognized catalog and explicit execution permissions, then follow its progress. */
async function submit() {
  if (!catalog.value || !origin.value.trim() || busy.value || running.value) return
  busy.value = true
  try {
    const record = await startTask({ catalog: catalog.value, origin: origin.value.trim(), allow_writes: allowWrites.value, observation_ms: observationMs.value, wall_time_seconds: wallTime.value })
    if (disposed) return
    run.value = record
    await refreshHistory()
    void openHistory(record.id)
  } catch (error) { toast.push('danger', '启动失败', describeError(error)) }
  finally { busy.value = false }
}
/** Stop polling before cancellation, preventing a late running snapshot from reverting the UI. */
async function cancel() {
  if (!run.value) return
  const id = run.value.id
  stopPolling()
  busy.value = true
  try { await cancelTask(id); await openHistory(id) }
  catch (error) { toast.push('danger', '取消失败', describeError(error)); void openHistory(id) }
  finally { busy.value = false }
}
/** Remove a completed history entry through the backend lifecycle guard. */
async function removeHistory(id: string) {
  try {
    await deleteTask(id)
    if (run.value?.id === id) { stopPolling(); run.value = null }
    await refreshHistory()
  } catch (error) { toast.push('danger', '删除失败', describeError(error)) }
}
onMounted(() => { void refreshHistory().catch(error => toast.push('danger', '历史加载失败', describeError(error))) })
onBeforeUnmount(() => { disposed = true; stopPolling() })
</script>

<template>
  <TestWorkspace persist-key="api-agent.layout.cases.history" active-pane="tasks">
    <template #history><HistoryList title="测试历史" empty-text="还没有文档测试任务" :items="entries" :active-id="run?.id" :on-delete="removeHistory" @select="openHistory" /></template>
    <ResizableSplit v-model:size="width" persist-key="api-agent.layout.tasks.input" workspace-pane="tasks" :min="300" :min-other="320" label="调整输入与结果宽度">
      <template #first>
        <section class="task-panel scroll-y">
          <h2><AppIcon name="workflow" /> 用例测试</h2>
          <label class="upload">上传 API 接口文档
            <input type="file" accept=".json,.yaml,.yml,.md,.txt" :disabled="busy || running" @change="upload" />
          </label>
          <p class="hint">支持 OpenAPI / Swagger JSON、YAML 和文本描述。非标准文档由 AI 识别；最大 512 KiB。</p>
          <p v-if="busy">正在处理，请稍候…</p>
          <template v-if="catalog">
            <strong>{{ filename }}</strong>
            <p>{{ catalog.endpoints.length }} 个接口 · {{ total }} 条用例（含文档原有用例）</p>
            <ul class="operations"><li v-for="(endpoint, index) in catalog.endpoints" :key="index"><code>{{ endpoint.protocol.toUpperCase() }} {{ endpoint.method }} {{ endpoint.path }}</code><span> {{ (endpoint.cases?.length ?? 0) + 1 + (endpoint.generated_cases?.length ?? 0) }} 用例</span></li></ul>
            <p v-for="warning in catalog.warnings" :key="warning" class="warning">{{ warning }}</p>
          </template>
          <label class="field">测试目标 origin<input v-model="origin" class="input" placeholder="http://127.0.0.1:9100" :disabled="running" /></label>
          <p class="hint">确认目标后开始测试；文档中的服务器地址不会自动作为执行目标。</p>
          <label class="check"><input v-model="allowWrites" type="checkbox" :disabled="running" />允许写接口和 WebSocket 发送消息</label>
          <details><summary>观察时限</summary>
            <label class="field">流观察时长（毫秒）<input v-model.number="observationMs" class="input" type="number" min="1000" max="30000" :disabled="running" /></label>
            <label class="field">任务总时长（秒）<input v-model.number="wallTime" class="input" type="number" min="10" max="3600" :disabled="running" /></label>
          </details>
          <div class="actions"><button class="btn btn-primary" :disabled="!catalog || !origin.trim() || busy || running" @click="submit"><AppIcon name="play" />开始测试</button><button v-if="running" class="btn" :disabled="busy" @click="cancel">取消任务</button></div>
        </section>
      </template>
      <template #second>
        <section class="task-panel scroll-y">
          <h2>执行结果 <a v-if="run" class="btn" :href="'/api/tasks/' + run.id + '/report'" download><AppIcon name="download" />导出总结</a></h2>
          <p v-if="!run" class="hint">上传文档并开始测试，逐条结果和 AI 总结将在这里显示。</p>
          <template v-else>
            <p>{{ labels[run.status] ?? run.status }} · {{ run.results.length }} / {{ run.snapshot.cases.length }} 条 · {{ run.stage === 'summarizing' ? '正在生成总结' : '内置契约引擎' }}</p>
            <p v-if="run.error_kind" class="warning">{{ run.error_kind }}；已记录结果仍可导出。</p>
            <details v-for="result in run.results" :key="result.index" class="case">
              <summary>{{ result.index + 1 }}. {{ result.name }} · {{ result.protocol }} · {{ labels[result.status] ?? result.status }} · {{ result.source === 'document' ? '原有用例' : '生成用例' }}</summary>
              <p v-if="result.reason">{{ result.reason }}</p>
              <p v-for="(assertion, index) in result.assertions" :key="index">{{ assertion.kind }}：{{ assertion.passed === null ? '未评估' : assertion.passed ? '通过' : '未通过' }} · 期望 {{ JSON.stringify(assertion.expected) }} · 实际 {{ JSON.stringify(assertion.actual) }}</p>
              <pre>{{ JSON.stringify(result.evidence, null, 2) }}</pre>
            </details>
            <h3>AI 总结</h3><pre class="summary">{{ run.summary || '总结尚未生成。流程完成不代表所有断言通过，请查看逐条结果。' }}</pre>
          </template>
        </section>
      </template>
    </ResizableSplit>
  </TestWorkspace>
</template>
<style scoped>
.task-panel { height: 100%; min-height: 0; padding: 18px; box-sizing: border-box; }
h2 { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 15px; margin: 0 0 16px; }
.field { display: flex; flex-direction: column; gap: 8px; margin: 16px 0; }
.upload { display: flex; flex-direction: column; gap: 12px; padding: 18px; border: 1px dashed var(--border-strong); border-radius: var(--radius-lg); background: var(--surface-gradient); margin-top: 18px; }
.hint { line-height: 1.7; color: var(--text-tertiary); }
.operations { max-height: 240px; overflow: auto; padding-left: 16px; }
.operations li { padding: 5px 0; overflow-wrap: anywhere; }
.actions { display: flex; gap: 8px; margin-top: 20px; }
.check { display: flex; align-items: center; gap: 8px; margin: 16px 0; }
.case { border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--surface-gradient); box-shadow: var(--shadow-sm); padding: 12px; margin: 10px 0; }
.case summary { cursor: pointer; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
.warning { color: var(--warning); }
.summary { line-height: 1.8; }
</style>
