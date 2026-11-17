<script setup lang="ts">
/** 项目源码与文档工作区。按项目挂载，展示完整扫描范围、后台进度、历史及安全文本预览。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Download, FileText, FolderInput, Loader2, RefreshCw, Sparkles, Square } from 'lucide-vue-next'
import { ApiError, describeError } from '@/api/client'
import {
  cancelProjectDocument, exportProjectDocument, importProjectSource, listProjectDocuments,
  readProjectDocument, readProjectSource, startProjectDocument,
} from '@/api/projectDocuments'
import type { DocumentRecord, SourceSummary } from '@/api/projectDocuments'
import { usePolling } from '@/composables/usePolling'
import { formatBytes, formatDateTime } from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

const props = defineProps<{ projectId: string }>()
const source = ref<SourceSummary | null>(null)
const documents = ref<DocumentRecord[]>([])
const current = ref<DocumentRecord | null>(null)
const directory = ref('')
const showImport = ref(false)
const loading = ref(true)
const importing = ref(false)
const starting = ref(false)
const cancelling = ref(false)
const exporting = ref(false)
const error = ref('')
const selectionError = ref('')
let disposed = false
let selection = 0
let detailController: AbortController | null = null
const initialController = new AbortController()
const isActive = computed(() => current.value ? activeRecord(current.value) : false)
const hasRunning = computed(() => documents.value.some(activeRecord))
const canGenerate = computed(() => !!source.value && source.value.scanned_files > 0 &&
  !loading.value && !importing.value && !starting.value && !hasRunning.value)
const progress = computed(() => {
  if (!current.value?.total_batches) return 0
  return Math.min(100, Math.round(current.value.processed_batches / current.value.total_batches * 100))
})

/** 只将排队与运行识别为活动状态，其余保存版本均可回看。 */
function activeRecord(record: DocumentRecord): boolean {
  return record.status === 'queued' || record.status === 'running'
}

/** 将后台状态映射为用户可以辨认的完成度。 */
function statusLabel(status: string): string {
  return ({ queued: '排队中', running: '生成中', completed: '已完成', partial: '部分完成',
    failed: '生成失败', cancelled: '已取消', interrupted: '已中断' } as Record<string, string>)[status] ?? status
}

/** 以 ID 合并进度，历史和当前预览始终指向同一保存版本。 */
function updateRecord(record: DocumentRecord): void {
  current.value = record
  const index = documents.value.findIndex(item => item.id === record.id)
  if (index >= 0) documents.value[index] = record
  else documents.value.unshift(record)
}

const polling = usePolling<DocumentRecord>({
  fetch: signal => readProjectDocument(props.projectId, current.value!.id, signal),
  onResult: record => { selectionError.value = ''; updateRecord(record) },
  onError: failure => { selectionError.value = describeError(failure) },
  isTerminal: record => !activeRecord(record),
  intervalMs: 1500,
})

/** 切换历史时中止旧请求，并丢弃晚到结果，防止旧文档覆盖当前选择。 */
async function openDocument(documentId: string): Promise<void> {
  polling.stop()
  detailController?.abort()
  detailController = new AbortController()
  const generation = ++selection
  selectionError.value = ''
  current.value = documents.value.find(item => item.id === documentId) ?? null
  try {
    const record = await readProjectDocument(props.projectId, documentId, detailController.signal)
    if (disposed || generation !== selection) return
    updateRecord(record)
    if (activeRecord(record)) polling.start()
  } catch (failure) {
    if (disposed || generation !== selection) return
    selectionError.value = describeError(failure)
  }
}

/** 独立加载快照与历史，首次导入的正常 404 只形成空态，其他失败保留重试入口。 */
async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  const results = await Promise.allSettled([
    readProjectSource(props.projectId, initialController.signal),
    listProjectDocuments(props.projectId, initialController.signal),
  ])
  if (disposed) return
  const [sourceResult, historyResult] = results
  const failures: string[] = []
  if (sourceResult.status === 'fulfilled') source.value = sourceResult.value
  else if (!(sourceResult.reason instanceof ApiError && sourceResult.reason.status === 404)) {
    failures.push(describeError(sourceResult.reason))
  }
  if (historyResult.status === 'fulfilled') {
    documents.value = historyResult.value.items
    const selected = documents.value.find(activeRecord) ?? documents.value[0]
    if (selected) await openDocument(selected.id)
  } else failures.push(describeError(historyResult.reason))
  if (disposed) return
  error.value = failures.join('；')
  showImport.value = !source.value
  loading.value = false
}

/** 读取用户指定目录并保存新快照，重新导入不改变历史文档使用的来源。 */
async function importSource(): Promise<void> {
  if (!directory.value.trim() || importing.value) return
  importing.value = true
  error.value = ''
  try {
    const imported = await importProjectSource(props.projectId, directory.value.trim())
    if (disposed) return
    source.value = imported
    showImport.value = false
    directory.value = ''
    useToastStore().push(imported.complete ? 'success' : 'warning', imported.complete ? '源码已导入' : '扫描存在未读取文件')
  } catch (failure) {
    if (!disposed) error.value = describeError(failure)
  } finally {
    if (!disposed) importing.value = false
  }
}

/** 生成请求固定当前源码 ID，后台接受后立即显示进度并开始轮询。 */
async function generate(): Promise<void> {
  if (!canGenerate.value || !source.value) return
  starting.value = true
  selectionError.value = ''
  try {
    const record = await startProjectDocument(props.projectId, source.value.id)
    if (disposed) return
    selection++
    detailController?.abort()
    polling.stop()
    updateRecord(record)
    if (activeRecord(record)) polling.start()
  } catch (failure) {
    if (!disposed) selectionError.value = describeError(failure)
  } finally {
    if (!disposed) starting.value = false
  }
}

/** 取消当前运行，选择已变化时只更新对应历史，避免跳回旧记录。 */
async function cancel(): Promise<void> {
  if (!current.value || cancelling.value) return
  const id = current.value.id
  cancelling.value = true
  try {
    const record = await cancelProjectDocument(props.projectId, id)
    if (disposed) return
    if (current.value?.id === id) {
      polling.stop()
      updateRecord(record)
      if (activeRecord(record)) polling.start()
    } else {
      const index = documents.value.findIndex(item => item.id === id)
      if (index >= 0) documents.value[index] = record
    }
  } catch (failure) {
    if (!disposed) useToastStore().push('danger', '取消失败', describeError(failure))
  } finally {
    if (!disposed) cancelling.value = false
  }
}

/** 先校验下载响应再创建浏览器文件，失败保留当前预览并显示管理端错误。 */
async function download(format: 'markdown' | 'openapi'): Promise<void> {
  if (!current.value || exporting.value) return
  const id = current.value.id
  exporting.value = true
  try {
    const content = await exportProjectDocument(props.projectId, id, format)
    if (disposed) return
    const text = typeof content === 'string' ? content : JSON.stringify(content, null, 2)
    const blob = new Blob([text], { type: format === 'markdown' ? 'text/markdown;charset=utf-8' : 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `api-document-${id}.${format === 'markdown' ? 'md' : 'json'}`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (failure) {
    if (!disposed) useToastStore().push('danger', '文档导出失败', describeError(failure))
  } finally {
    if (!disposed) exporting.value = false
  }
}

onMounted(load)
/** 项目切换或页面关闭时释放请求与轮询，后台生成由服务端继续管理。 */
onBeforeUnmount(() => {
  disposed = true
  selection++
  initialController.abort()
  detailController?.abort()
})
</script>

<template>
  <div class="project-documents">
    <section class="document-card">
      <div class="card-heading">
        <h2>项目源码</h2>
        <button class="btn btn-sm" :disabled="loading || importing" @click="showImport = !showImport">
          <FolderInput :size="13" />{{ source ? '重新导入源码' : '导入源码' }}
        </button>
      </div>
      <p v-if="loading" class="hint"><Loader2 :size="13" class="spinning" /> 正在读取项目资料</p>
      <div v-if="error" class="error-message" role="alert">
        {{ error }}
        <button class="btn btn-sm" :disabled="loading" @click="load"><RefreshCw :size="12" />重新加载</button>
      </div>
      <form v-if="showImport" class="source-form" @submit.prevent="importSource">
        <label class="field">
          <span class="field-label">项目目录</span>
          <input v-model="directory" class="input mono" placeholder="运行 API-Agent 电脑上的项目绝对路径" required :disabled="importing" />
        </label>
        <p class="hint">递归读取项目源码并保存扫描快照，后续生成使用这份快照。</p>
        <button class="btn btn-primary" :disabled="importing || !directory.trim()">
          <Loader2 v-if="importing" :size="13" class="spinning" />
          <FolderInput v-else :size="13" />{{ importing ? '扫描项目中' : '导入并扫描' }}
        </button>
      </form>
      <template v-if="source">
        <div class="scan-summary">
          <span class="tag" :class="source.complete ? 'tag-success' : 'tag-warning'">{{ source.complete ? '源码扫描完成' : '源码扫描不完整' }}</span>
          <span>已读取 {{ source.scanned_files }} 个源码文件</span>
          <span>{{ source.endpoints.length }} 个静态接口</span>
          <span>{{ formatBytes(source.total_bytes) }}</span>
        </div>
        <p class="hint">导入于 {{ formatDateTime(source.created_at) }} · 共发现 {{ source.total_files }} 个文件 · 已排除 {{ source.excluded_files }} 个文件或目录</p>
        <p v-if="!source.complete" class="warning-message">部分源码未读取，本次生成将标记为部分完成，并在导出文档中列出遗漏项。可检查扫描问题后重新导入。</p>
        <details v-if="source.issues.length || source.warnings.length" class="scan-details">
          <summary>查看扫描范围与待核验项</summary>
          <ul v-if="source.warnings.length"><li v-for="warning in source.warnings" :key="warning">{{ warning }}</li></ul>
          <div v-for="(issue, index) in source.issues" :key="index" class="scan-issue"><code>{{ issue.path }}</code><span>{{ issue.reason }}</span></div>
        </details>
      </template>
    </section>

    <section class="document-card">
      <div class="card-heading">
        <h2>AI 接口文档</h2>
        <button class="btn btn-accent" :disabled="!canGenerate" @click="generate">
          <Loader2 v-if="starting" :size="13" class="spinning" />
          <Sparkles v-else :size="13" />生成接口文档
        </button>
      </div>
      <p class="generation-hint">生成时会将导入的脱敏源码分批发送给系统设置中的模型，逐批分析后汇总接口、参数、请求体、响应、鉴权及源码依据。</p>
      <p v-if="!source && !loading" class="hint">先导入项目源码，随后可生成并导出接口文档。</p>
      <label v-if="documents.length" class="history-picker field">
        <span class="field-label">生成历史</span>
        <select class="select" :value="current?.id ?? ''" @change="openDocument(($event.target as HTMLSelectElement).value)">
          <option v-for="record in documents" :key="record.id" :value="record.id">
            {{ formatDateTime(record.created_at) }} · {{ statusLabel(record.status) }} · {{ record.endpoints.length }} 个接口
          </option>
        </select>
      </label>
      <div v-if="selectionError" class="error-message" role="alert">
        {{ selectionError }}
        <button v-if="current" class="btn btn-sm" @click="openDocument(current.id)">重新读取</button>
      </div>
      <template v-if="current">
        <div class="document-status" aria-live="polite">
          <div class="scan-summary">
            <span class="tag" :class="current.status === 'completed' ? 'tag-success' : 'tag-warning'">{{ statusLabel(current.status) }}</span>
            <span>{{ current.processed_files }} / {{ current.total_files }} 个文件</span>
            <span>{{ current.processed_batches }} / {{ current.total_batches }} 批</span>
            <span>{{ current.endpoints.length }} 个接口</span>
          </div>
          <button v-if="isActive" class="btn btn-sm" :disabled="cancelling" @click="cancel"><Square :size="12" />取消生成</button>
        </div>
        <progress v-if="isActive" class="generation-progress" :value="progress" max="100" aria-label="文档生成进度" />
        <p v-if="source && current.source_id !== source.id" class="hint">这份文档使用此前导入的源码快照。</p>
        <p v-if="current.error" class="error-message" role="alert">{{ current.error }}</p>
        <details v-if="current.warnings.length" class="scan-details">
          <summary>生成说明与待核验项（{{ current.warnings.length }}）</summary>
          <ul><li v-for="warning in current.warnings" :key="warning">{{ warning }}</li></ul>
        </details>
        <div class="export-actions">
          <button class="btn btn-sm" :disabled="exporting || !current.markdown || isActive" @click="download('markdown')"><Download :size="13" />导出 Markdown</button>
          <button class="btn btn-sm" :disabled="exporting || !current.markdown || isActive" @click="download('openapi')"><Download :size="13" />导出 OpenAPI JSON</button>
          <span v-if="!isActive && current.status !== 'completed'" class="hint">导出包含当前已生成内容及完成度说明</span>
        </div>
        <pre v-if="current.markdown" class="document-preview" aria-label="接口文档预览">{{ current.markdown }}</pre>
        <p v-else-if="isActive" class="hint">正在分析源码，已完成批次的接口会计入上方进度。</p>
        <p v-else class="hint"><FileText :size="13" /> 当前记录还没有生成文档正文。</p>
      </template>
    </section>
  </div>
</template>

<style scoped>
.project-documents { display: flex; flex-direction: column; gap: 14px; }
.document-card { padding: 16px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--surface-gradient); box-shadow: var(--shadow-sm); min-width: 0; }
.card-heading, .document-status { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.card-heading { margin-bottom: 12px; }
h2 { margin: 0; font-size: 13px; font-weight: 650; }
.source-form { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; margin: 12px 0; }
.source-form .field { width: 100%; }
.source-form .hint { margin: 0; }
.scan-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; font-size: 12px; }
.scan-details { margin-top: 12px; color: var(--text-secondary); font-size: 12px; max-height: 240px; overflow: auto; }
.scan-details summary { cursor: pointer; }
.scan-details ul { padding-left: 20px; }
.scan-issue { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 0; overflow-wrap: anywhere; }
.scan-issue code { color: var(--text-primary); }
.generation-hint { margin: 0 0 14px; color: var(--text-secondary); font-size: 12px; line-height: 1.75; }
.history-picker { margin-bottom: 14px; }
.history-picker .select { width: 100%; }
.generation-progress { width: 100%; height: 7px; margin-top: 12px; accent-color: var(--accent); }
.export-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 16px 0 12px; }
.document-preview { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; padding: 16px; max-height: 600px; overflow-y: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-sunken); line-height: 1.8; font-size: 12px; }
.error-message, .warning-message { padding: 10px 12px; margin: 10px 0; border-radius: var(--radius-sm); font-size: 12px; overflow-wrap: anywhere; border: 1px solid transparent; }
.error-message { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-border); }
.error-message .btn { margin-left: 8px; }
.warning-message { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-border); }
.spinning { animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
