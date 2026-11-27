<script setup lang="ts">
/**
 * AI 助手小Y 向量知识库设置与一键构建面板：
 * 展示已预置的项目全功能语料、配置 Embedding 模型，并在接入向量模型后一键触发向量构建。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  AlertCircle,
  BookOpen,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  Loader2,
  Save,
  Sparkles,
} from 'lucide-vue-next'
import { describeError } from '@/api/client'
import {
  buildKnowledgeIndex,
  getKnowledgeStatus,
  updateKnowledgeConfig,
} from '@/api/endpoints'
import type { KnowledgeStatus } from '@/api/types'
import { useToastStore } from '@/stores/toasts'

const EMBEDDING_PRESETS = [
  { label: '阿里千问 v4', model: 'text-embedding-v4' },
  { label: 'OpenAI Small', model: 'text-embedding-3-small' },
  { label: 'BGE-M3', model: 'BAAI/bge-m3' },
  { label: 'Ollama Nomic', model: 'nomic-embed-text' },
  { label: '智谱 Embedding-3', model: 'embedding-3' },
]

const loading = ref(false)
const saving = ref(false)
const building = ref(false)
const showKey = ref(false)
const status = ref<KnowledgeStatus | null>(null)

const form = reactive({
  enabled: true,
  useLlmEndpoint: true,
  baseUrl: '',
  apiKey: '',
  model: '',
  dimensions: '' as string | number,
  topK: 4,
  timeoutSeconds: 30,
})

const statusBadge = computed(() => {
  const current = status.value?.status
  if (current === 'ready') return { label: '向量索引就绪', tone: 'tag-success' }
  if (current === 'unbuilt') return { label: '待构建向量', tone: 'tag-info' }
  if (current === 'stale') return { label: '需重建向量', tone: 'tag-warning' }
  return { label: '语料已就绪 · 待配向量模型', tone: 'tag-neutral' }
})

const retrievalLabel = computed(() =>
  status.value?.retrieval_mode === 'vector' ? 'Chroma 向量语义检索' : '本地词法回退（未构建向量前可用）',
)

const vectorStoreLabel = computed(() =>
  (status.value?.vector_store || 'chroma').toLowerCase() === 'chroma'
    ? 'ChromaDB (本地持久化)'
    : status.value?.vector_store || 'ChromaDB',
)

const canBuild = computed(() => {
  if (!form.enabled || !form.model.trim()) return false
  if (form.useLlmEndpoint) return Boolean(status.value?.config.effective_base_url || form.baseUrl.trim())
  return Boolean(form.baseUrl.trim())
})

function applyStatus(data: KnowledgeStatus): void {
  status.value = data
  form.enabled = data.config.enabled
  form.useLlmEndpoint = data.config.use_llm_endpoint
  form.baseUrl = data.config.base_url ?? ''
  form.model = data.config.model ?? ''
  form.dimensions = data.config.dimensions ?? ''
  form.topK = data.config.top_k ?? 4
  form.timeoutSeconds = data.config.timeout_seconds ?? 30
  form.apiKey = ''
}

async function loadStatus(): Promise<void> {
  loading.value = true
  try {
    applyStatus(await getKnowledgeStatus())
  } catch (error) {
    useToastStore().push('danger', '读取知识库状态失败', describeError(error))
  } finally {
    loading.value = false
  }
}

function buildUpdatePayload() {
  const rawDim = String(form.dimensions ?? '').trim()
  const parsedDim = rawDim ? Number(rawDim) : null
  const dimensions = parsedDim && Number.isInteger(parsedDim) && parsedDim >= 16 && parsedDim <= 8192 ? parsedDim : null
  return {
    enabled: form.enabled,
    use_llm_endpoint: form.useLlmEndpoint,
    base_url: form.baseUrl.trim() || null,
    api_key: form.apiKey.trim() ? form.apiKey.trim() : undefined,
    model: form.model.trim(),
    dimensions,
    top_k: Math.max(1, Math.min(10, Number(form.topK) || 4)),
    timeout_seconds: Math.max(3, Math.min(180, Number(form.timeoutSeconds) || 30)),
  }
}

async function saveConfig(silent = false): Promise<boolean> {
  saving.value = true
  try {
    const updated = await updateKnowledgeConfig(buildUpdatePayload())
    applyStatus(updated)
    if (!silent) {
      useToastStore().push('success', '向量知识库配置已保存', updated.configured ? '可直接点击「构建向量知识库」' : '配置好向量模型后即可一键构建')
    }
    return true
  } catch (error) {
    useToastStore().push('danger', '保存向量配置失败', describeError(error))
    return false
  } finally {
    saving.value = false
  }
}

async function buildVectors(): Promise<void> {
  if (!form.model.trim()) {
    useToastStore().push('warning', '请先填写向量模型名称', '例如 text-embedding-3-small、BAAI/bge-m3 或 nomic-embed-text')
    return
  }
  const saved = await saveConfig(true)
  if (!saved) return
  building.value = true
  try {
    const result = await buildKnowledgeIndex()
    await loadStatus()
    useToastStore().push('success', '向量知识库构建完成', `${result.message}（耗时 ${result.duration_ms}ms）`)
  } catch (error) {
    await loadStatus()
    useToastStore().push('danger', '构建向量失败', describeError(error))
  } finally {
    building.value = false
  }
}

onMounted(() => { void loadStatus() })
</script>

<template>
  <section class="kb-settings" aria-labelledby="kb-settings-title">
    <div class="kb-head">
      <div class="kb-title-row">
        <h3 id="kb-settings-title">小Y 项目功能向量知识库</h3>
        <span class="tag" :class="statusBadge.tone">
          <CheckCircle2 v-if="status?.ready" :size="12" />
          <AlertCircle v-else :size="12" />
          <span>{{ statusBadge.label }}</span>
        </span>
      </div>
      <p class="kb-sub">
        已完整预置本项目全部功能介绍与使用方法分块。未接入向量模型时自动使用本地词法回退；配置好向量模型后点击「构建向量知识库」即可生成并启用向量语义检索。
      </p>
    </div>

    <div v-if="status" class="kb-stats">
      <div class="kb-stat">
        <span class="kb-stat-label">向量库引擎</span>
        <strong>{{ vectorStoreLabel }}</strong>
      </div>
      <div class="kb-stat">
        <span class="kb-stat-label">已准备主题文档</span>
        <strong>{{ status.documents.length }} 篇</strong>
      </div>
      <div class="kb-stat">
        <span class="kb-stat-label">知识分块 / 向量数</span>
        <strong>{{ status.chunk_count }} / {{ status.vector_count }}</strong>
      </div>
      <div class="kb-stat">
        <span class="kb-stat-label">向量维度</span>
        <strong>{{ status.embedding_dimensions ?? '待构建' }}</strong>
      </div>
      <div class="kb-stat">
        <span class="kb-stat-label">当前检索模式</span>
        <strong>{{ retrievalLabel }}</strong>
      </div>
    </div>

    <div class="kb-presets">
      <span class="kb-presets-label">常用向量模型：</span>
      <button
        v-for="preset in EMBEDDING_PRESETS"
        :key="preset.model"
        type="button"
        class="kb-chip"
        @click="form.model = preset.model"
      >
        {{ preset.label }}
      </button>
    </div>

    <div class="kb-grid">
      <label class="kb-toggle">
        <input v-model="form.enabled" type="checkbox" class="switch" />
        <span>启用向量知识库</span>
      </label>
      <label class="kb-toggle">
        <input v-model="form.useLlmEndpoint" type="checkbox" class="switch" />
        <span>复用上方大模型接口地址与密钥</span>
      </label>

      <label v-if="!form.useLlmEndpoint" class="kb-field kb-full">
        <span>向量服务 Base URL</span>
        <input
          v-model="form.baseUrl"
          type="text"
          class="input mono"
          placeholder="https://api.openai.com/v1 或 http://127.0.0.1:11434/v1"
          spellcheck="false"
        />
      </label>

      <label v-if="!form.useLlmEndpoint" class="kb-field kb-full">
        <span>向量服务 API Key（留空保留已存密钥）</span>
        <div class="kb-key-row">
          <input
            v-model="form.apiKey"
            :type="showKey ? 'text' : 'password'"
            class="input mono"
            :placeholder="status?.config.has_api_key ? '已保存密钥，留空保持不变' : '可选，例如 sk-...'"
            spellcheck="false"
            autocomplete="off"
          />
          <button type="button" class="btn btn-ghost btn-sm" @click="showKey = !showKey">
            <EyeOff v-if="showKey" :size="14" />
            <Eye v-else :size="14" />
          </button>
        </div>
      </label>

      <label class="kb-field">
        <span>向量模型名称 (Embedding Model)</span>
        <input
          v-model="form.model"
          type="text"
          class="input mono"
          placeholder="text-embedding-3-small / BAAI/bge-m3"
          spellcheck="false"
        />
      </label>

      <label class="kb-field">
        <span>召回片段数 Top-K (1-10)</span>
        <input v-model.number="form.topK" type="number" class="input" min="1" max="10" />
      </label>

      <label class="kb-field">
        <span>可选向量维度（留空用模型默认）</span>
        <input v-model="form.dimensions" type="number" class="input" min="16" max="8192" placeholder="自动" />
      </label>

      <label class="kb-field">
        <span>请求超时 (秒)</span>
        <input v-model.number="form.timeoutSeconds" type="number" class="input" min="3" max="180" />
      </label>
    </div>

    <p v-if="status?.last_error" class="kb-error" role="alert">
      上次构建记录：{{ status.last_error }}
    </p>

    <details v-if="status?.documents.length" class="kb-docs">
      <summary>
        <BookOpen :size="13" />
        <span>查看已准备的项目功能知识目录（{{ status.documents.length }} 篇 / {{ status.chunk_count }} 块）</span>
      </summary>
      <ul class="kb-doc-list">
        <li v-for="doc in status.documents" :key="doc.doc_id" class="kb-doc-item">
          <span class="kb-doc-cat">{{ doc.category }}</span>
          <span class="kb-doc-title">{{ doc.title }}</span>
          <span class="kb-doc-meta">{{ doc.sections.join(' · ') }}</span>
        </li>
      </ul>
    </details>

    <div class="kb-actions">
      <button type="button" class="btn btn-ghost btn-sm" :disabled="saving || building || loading" @click="saveConfig(false)">
        <Loader2 v-if="saving" :size="13" class="spinning" />
        <Save v-else :size="13" />
        <span>{{ saving ? '保存中...' : '保存向量配置' }}</span>
      </button>
      <button
        type="button"
        class="btn btn-accent btn-sm"
        :disabled="building || saving || loading || !canBuild"
        :title="canBuild ? '立即调用 /embeddings 为全部项目功能文档构建向量索引' : '请先填写向量模型名称并确保接口地址可用'"
        @click="buildVectors"
      >
        <Loader2 v-if="building" :size="13" class="spinning" />
        <Database v-else-if="status?.ready" :size="13" />
        <Sparkles v-else :size="13" />
        <span>{{ building ? '构建向量中...' : status?.ready ? '重建向量知识库' : '构建向量知识库' }}</span>
      </button>
    </div>
  </section>
</template>

<style scoped>
.kb-settings { display: grid; gap: 14px; }
.kb-head { display: grid; gap: 6px; }
.kb-title-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.kb-title-row h3 { margin: 0; font-size: 13.5px; font-weight: 650; }
.kb-sub { margin: 0; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }
.kb-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.kb-stat { display: grid; gap: 3px; padding: 8px 10px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-subtle); }
.kb-stat-label { color: var(--text-tertiary); font-size: 11px; }
.kb-stat strong { font-size: 12.5px; color: var(--text-primary); }
.kb-presets { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.kb-presets-label { color: var(--text-secondary); font-size: 11.5px; }
.kb-chip { height: 24px; padding: 0 9px; border: 1px solid var(--border-default); border-radius: 999px; background: var(--bg-subtle); color: var(--text-secondary); font-size: 11px; }
.kb-chip:hover { color: var(--text-primary); border-color: var(--accent-border); background: var(--accent-soft); }
.kb-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.kb-full { grid-column: 1 / -1; }
.kb-toggle { display: inline-flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 12px; cursor: pointer; }
.kb-field { display: grid; gap: 5px; font-size: 12px; color: var(--text-secondary); }
.kb-key-row { display: flex; gap: 6px; }
.kb-key-row .input { flex: 1; min-width: 0; }
.kb-error { margin: 0; padding: 8px 10px; border: 1px solid var(--danger-border); border-radius: var(--radius-sm); background: var(--danger-soft); color: var(--danger); font-size: 12px; }
.kb-docs { padding: 8px 10px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-subtle); }
.kb-docs summary { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; color: var(--text-secondary); font-size: 12px; font-weight: 500; }
.kb-doc-list { list-style: none; margin: 10px 0 0; padding: 0; display: grid; gap: 6px; }
.kb-doc-item { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 8px; padding: 6px 8px; border-radius: var(--radius-sm); background: var(--bg-surface); border: 1px solid var(--border-subtle); font-size: 11.5px; }
.kb-doc-cat { color: var(--accent); font-weight: 600; }
.kb-doc-title { color: var(--text-primary); font-weight: 600; }
.kb-doc-meta { grid-column: 1 / -1; color: var(--text-tertiary); font-size: 11px; }
.kb-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding-top: 4px; }
@media (max-width: 640px) {
  .kb-grid { grid-template-columns: 1fr; }
}
</style>
