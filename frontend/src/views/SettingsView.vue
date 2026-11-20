<script setup lang="ts">
/** 系统设置页：依赖 LLM 配置接口与共享引擎选择，维护模型表单、探测状态和引擎详情弹窗。保存密钥后清空输入，不回显已存明文；引擎导入、配置保存与退出入口仍为界面反馈。 */

import { computed, onMounted, reactive, ref } from 'vue'
import {
  AlertCircle,
  Blocks,
  CheckCircle2,
  Download,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  LogOut,
  Radio,
  RefreshCw,
  RotateCw,
  Settings2,
  Save,
  Server,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  Terminal,
  Trash2,
  Wrench,
  Zap,
} from 'lucide-vue-next'
import AppModal from '@/components/AppModal.vue'
import CopyButton from '@/components/CopyButton.vue'
import TestSettingsFields from '@/components/TestSettingsFields.vue'
import ThemePaletteSettings from '@/components/ThemePaletteSettings.vue'
import { describeError } from '@/api/client'
import {
  getLLMConfig,
  getLLMModels,
  pingLLM,
  updateLLMConfig,
} from '@/api/endpoints'
import type { LLMConfig } from '@/api/types'
import { engineMeta, useEngineSelection } from '@/lib/engines'
import { useToastStore } from '@/stores/toasts'

const PRESETS = [
  {
    name: 'DeepSeek',
    url: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
    tag: '推荐',
  },
  {
    name: 'OpenAI 官方',
    url: 'https://api.openai.com/v1',
    model: 'gpt-4o',
    tag: '国际',
  },
  {
    name: '智谱 GLM',
    url: 'https://open.bigmodel.cn/api/paas/v4',
    model: 'glm-4-plus',
    tag: '国内',
  },
  {
    name: 'Ollama (本地)',
    url: 'http://127.0.0.1:11434/v1',
    model: 'qwen2.5:7b',
    tag: '无需密钥',
  },
  {
    name: 'OneAPI / 聚合中转',
    url: 'http://127.0.0.1:3000/v1',
    model: 'gpt-4o',
    tag: '自建',
  },
]

const form = reactive({
  enabled: false,
  baseUrl: '',
  apiKey: '',
  model: 'gpt-4o',
  timeoutSeconds: 60,
})

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const showKey = ref(false)
const hasSavedKey = ref(false)

const pingStatus = ref<{
  tested: boolean
  ok: boolean
  latencyMs: number
  message: string
} | null>(null)

/** 仅判断当前表单是否填写 Base URL；不表示配置已保存或服务可连通。 */
const isConfigured = computed(() => !!form.baseUrl.trim())

/** 读取已保存模型配置填入表单，密钥仅保留存在标志且清空明文输入；失败提示并解除 loading。 */
async function loadSettings() {
  loading.value = true
  try {
    const config: LLMConfig = await getLLMConfig()
    form.enabled = config.enabled
    form.baseUrl = config.base_url ?? ''
    form.model = config.model ?? 'gpt-4o'
    form.timeoutSeconds = config.timeout_seconds ?? 60
    hasSavedKey.value = config.has_api_key
    form.apiKey = '' // 绝不在前端回显真实密钥明文
  } catch (error) {
    useToastStore().push('danger', '获取模型配置失败', describeError(error))
  } finally {
    loading.value = false
  }
}

/** 从下拉框选定模型后回写表单并提示。 */
function onModelPicked(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  form.model = value
  useToastStore().push('info', `已选择模型 ${value}`, '记得点击「保存模型配置」使其生效')
}

/** 切换密钥明文/密文，并给出与当前状态一致的提示。 */
function toggleKeyVisibility(): void {
  showKey.value = !showKey.value
  useToastStore().push(
    'info',
    showKey.value ? '密钥已明文显示' : '密钥已隐藏',
    showKey.value ? '请确认周围无人，显示状态不会保存' : undefined,
  )
}

/** 将指定预设的地址和模型填入表单并清理旧探测结果；仅本地编辑，需要保存后才生效。 */
function applyPreset(preset: (typeof PRESETS)[number]) {
  form.baseUrl = preset.url
  form.model = preset.model
  pingStatus.value = null
  useToastStore().push('info', `已填入 ${preset.name} 预设`, `Base URL 与推荐模型已就绪`)
}

/** 通过后端使用当前表单探测模型连通性；空地址拦截，接口或探测失败写入失败状态并提示，始终解除 testing。 */
async function testConnection() {
  if (!form.baseUrl.trim()) {
    useToastStore().push('warning', '请先填写接口 Base URL')
    return
  }
  testing.value = true
  pingStatus.value = null
  try {
    const res = await pingLLM({
      base_url: form.baseUrl.trim(),
      api_key: form.apiKey.trim() || undefined,
      model: form.model.trim() || undefined,
    })
    pingStatus.value = {
      tested: true,
      ok: res.ok,
      latencyMs: res.latency_ms,
      message: res.message ?? (res.ok ? '服务连通正常' : '连通失败'),
    }
    if (res.ok) {
      useToastStore().push(
        'success',
        '大模型接口连接成功',
        `响应延迟 ${res.latency_ms}ms · ${res.message ?? ''}`,
      )
    } else {
      useToastStore().push('danger', '接口连通失败', res.message ?? undefined)
    }
  } catch (error) {
    pingStatus.value = {
      tested: true,
      ok: false,
      latencyMs: 0,
      message: describeError(error),
    }
    useToastStore().push('danger', '连通性测试异常', describeError(error))
  } finally {
    testing.value = false
  }
}

const fetchingModels = ref(false)
const availableModels = ref<string[]>([])

/** 按当前地址与可选新密钥读取可用模型；空地址拦截，空列表和服务失败分别提示，始终解除加载态。 */
async function fetchModels() {
  if (!form.baseUrl.trim()) {
    useToastStore().push('warning', '请先填写接口 Base URL')
    return
  }
  fetchingModels.value = true
  try {
    const res = await getLLMModels({
      base_url: form.baseUrl.trim(),
      api_key: form.apiKey.trim() || undefined,
    })
    if (res.ok && res.models.length > 0) {
      availableModels.value = res.models
      useToastStore().push('success', '获取模型列表成功', `从服务成功获取到 ${res.count} 个可用模型`)
    } else if (res.ok) {
      availableModels.value = []
      useToastStore().push('warning', '服务未返回任何模型', res.message ?? '')
    } else {
      useToastStore().push('danger', '获取模型列表失败', res.message ?? '')
    }
  } catch (error) {
    useToastStore().push('danger', '获取模型列表异常', describeError(error))
  } finally {
    fetchingModels.value = false
  }
}

/** 经浏览器确认后发送空密钥清除已保存值，同时提交当前表单配置；失败提示，成功只回显密钥存在状态。 */
async function clearApiKey() {
  if (!confirm('确定要清空已在本地保存的 API Key 吗？')) return
  try {
    const updated = await updateLLMConfig({
      enabled: form.enabled,
      provider: 'openai',
      base_url: form.baseUrl.trim() || null,
      api_key: '', // 传空串通知后端置空
      model: form.model.trim() || 'gpt-4o',
      timeout_seconds: form.timeoutSeconds,
    })
    hasSavedKey.value = updated.has_api_key
    form.apiKey = ''
    useToastStore().push('info', '已清空 API Key', '已从本地数据库中清除保存的密钥')
  } catch (error) {
    useToastStore().push('danger', '清空密钥失败', describeError(error))
  }
}

/** 保存当前模型配置；空密钥输入以 undefined 保留旧值，成功清空明文输入，失败提示并解除 saving。 */
async function saveSettings() {
  saving.value = true
  try {
    const updated = await updateLLMConfig({
      enabled: form.enabled,
      provider: 'openai',
      base_url: form.baseUrl.trim() || null,
      api_key: form.apiKey.trim() ? form.apiKey.trim() : undefined,
      model: form.model.trim() || 'gpt-4o',
      timeout_seconds: form.timeoutSeconds,
    })
    hasSavedKey.value = updated.has_api_key
    form.apiKey = '' // 提交后清空输入框，显示已保存状态
    useToastStore().push('success', '设置已保存', '配置已持久化至本地 SQLite 数据库')
  } catch (error) {
    useToastStore().push('danger', '保存失败', describeError(error))
  } finally {
    saving.value = false
  }
}

/** 退出登录：当前仅做交互响应与视觉反馈，不做具体账号注销或跳转 */
function handleLogout() {
  useToastStore().push(
    'info',
    '已退出当前工作会话',
    '当前系统运行在本地单机模式，无远程账号状态。',
  )
}

/* ---------- 测试引擎选择 ---------- */
/** 引擎选择与元信息统一来自 `lib/engines`，两页共享同一份存储。 */
const {
  engines,
  loading: enginesLoading,
  selected: activeEngineId,
  activeEngine,
  refresh: refreshEnginesBase,
  select: selectEngineBase,
} = useEngineSelection()

/** 选中引擎时给出可见反馈（纯提示，不改变选择语义）。 */
function selectEngine(id: string) {
  selectEngineBase(id)
  useToastStore().push(
    'info',
    `已选用「${engineMeta(id).name}」`,
    '「用例计划」页下次打开时将默认使用该引擎',
  )
}

/** 导入引擎：本轮只做界面占位，尚未实现具体导入逻辑。 */
function importEngine() {
  useToastStore().push(
    'info',
    '导入引擎（功能预览）',
    '支持接入自定义 MCP 测试引擎的能力正在规划中，当前仅展示入口。',
  )
}

/* ---------- 引擎配置弹窗 ---------- */
const configOpen = ref(false)
const configEngineId = ref<string>('')

/** 按弹窗所选 ID 读取引擎显示名；未知 ID 使用共享元信息回退。 */
const configEngineName = computed(() => engineMeta(configEngineId.value).name)
/** 当前弹窗对应的引擎条目（版本、可用性等）。 */
const configEngine = computed(
  () => engines.value.find((item) => item.id === configEngineId.value) ?? null,
)
/** 读取弹窗引擎的可用版本字符串；未加载或未报告时为空。 */
const configEngineVersion = computed(() => configEngine.value?.version ?? '')

/** 打开某个引擎的配置弹窗（每个引擎卡片右上角都有入口）。 */
function openEngineConfig(engine: { id: string }): void {
  configEngineId.value = engine.id
  configOpen.value = true
}

/** 关闭引擎配置弹窗；不保存配置或变更当前引擎。 */
function closeEngineConfig(): void {
  configOpen.value = false
}

/** 保存按钮当前为预留：只给出反馈，暂不写后端配置。 */
function saveEngineConfig(): void {
  const name = configEngineName.value
  configOpen.value = false
  useToastStore().push(
    'success',
    `${name} 配置已保存`,
    '当前为预留入口：配置项尚未持久化到后端',
  )
}

/** 刷新共享引擎列表，可按 showToast 展示就绪数量或错误；错误在此消费，初次静默刷新不弹出提示。 */
async function refreshEngines(showToast = false) {
  try {
    await refreshEnginesBase()
    if (showToast) {
      const ready = engines.value.filter((item) => item.available).length
      useToastStore().push(
        'success',
        '引擎状态已刷新',
        `${ready}/${engines.value.length} 个引擎在当前环境就绪`,
      )
    }
  } catch (error) {
    if (showToast) {
      useToastStore().push('danger', '引擎探测失败', describeError(error))
    }
  }
}


/** 生成本地 MCP 接入配置示例供复制；只输出非秘密模板，不安装、连接或修改服务配置。 */
const mcpConfigJson = computed(() => {
  return JSON.stringify(
    {
      mcpServers: {
        postmanmcp: {
          command: 'uv',
          args: ['run', '--project', 'backend', '--locked', 'api-agent-mcp'],
          env: {
            TARGET_ALLOWED_ORIGINS: '["http://127.0.0.1:8080"]',
            OMIT_RESPONSE_PAYLOAD: 'false',
          },
        },
      },
    },
    null,
    2,
  )
})

/** 挂载后独立加载模型配置与引擎列表；各任务自行处理失败，不发起模型探测。 */
onMounted(() => {
  loadSettings()
  refreshEngines()
})
</script>

<template>
  <div class="settings-page scroll-y">
    <div class="settings-container">
      <div class="page-head">
        <div class="head-icon">
          <SettingsIcon :size="20" />
        </div>
        <div>
          <h1 class="page-title">系统设置</h1>
          <p class="page-desc">
            配置 OpenAI 兼容的大模型服务，选择测试执行引擎，并管理内置 MCP 工具服务与本地工作台状态。
          </p>
        </div>
      </div>

      <!-- 第一部分：大模型配置 -->
      <section class="card" aria-labelledby="test-workbench-settings">
        <div class="card-header">
          <div class="card-title-group">
            <div>
              <h2 id="test-workbench-settings" class="card-title">工作台设置</h2>
              <p class="card-sub">与测试页共用配置，修改后自动保存在当前浏览器。</p>
            </div>
          </div>
        </div>
        <div class="card-body workbench-settings">
          <fieldset><legend>接口测试</legend><TestSettingsFields group="console" /></fieldset>
          <fieldset><legend>长连接测试</legend><TestSettingsFields group="websocket" /></fieldset>
          <fieldset><legend>智能测试</legend><TestSettingsFields group="task" /></fieldset>
        </div>
      </section>

      <section class="card" aria-labelledby="appearance-settings-title">
        <div class="card-header">
          <div class="card-title-group">
            <Settings2 :size="17" class="accent-icon" />
            <div>
              <h2 id="appearance-settings-title" class="card-title">外观与配色</h2>
              <p class="card-sub">自定义亮色和暗色主题；修改会应用到全局，重置恢复项目当前默认配色。</p>
            </div>
          </div>
        </div>
        <div class="card-body"><ThemePaletteSettings /></div>
      </section>

      <section class="card">
        <div class="card-header">
          <div class="card-title-group">
            <Sparkles :size="17" class="accent-icon" />
            <div>
              <h2 class="card-title">大模型服务 (OpenAI 兼容协议)</h2>
              <p class="card-sub">
                支持通用 OpenAI 兼容协议的 Response 接口（支持 Function/Tool Calling），包括 DeepSeek、智谱、Ollama、OneAPI 等。
              </p>
            </div>
          </div>
          <label class="switch-control" title="切换启用状态">
            <span class="switch-label">{{ form.enabled ? '已启用' : '已停用' }}</span>
            <input v-model="form.enabled" type="checkbox" class="switch-input" />
            <span class="switch-slider" />
          </label>
        </div>

        <div class="card-body">
          <!-- 常用预设芯片 -->
          <div class="presets-section">
            <span class="presets-label">常用服务一键预设：</span>
            <div class="presets-list">
              <button
                v-for="preset in PRESETS"
                :key="preset.name"
                type="button"
                class="preset-chip"
                @click="applyPreset(preset)"
              >
                <span class="preset-name">{{ preset.name }}</span>
                <span class="preset-tag">{{ preset.tag }}</span>
              </button>
            </div>
          </div>

          <!-- 表单网格 -->
          <div class="form-grid">
            <!-- Base URL -->
            <label class="field field-full">
              <span class="field-label-row">
                <span class="field-label">接口 Base URL <span class="required">*</span></span>
                <span class="field-hint">支持任何兼容 OpenAI 规范的端点</span>
              </span>
              <div class="input-with-icon">
                <Server :size="15" class="input-prefix-icon" />
                <input
                  v-model="form.baseUrl"
                  type="text"
                  class="input mono with-icon"
                  placeholder="https://api.openai.com/v1 或 https://api.deepseek.com/v1"
                  spellcheck="false"
                />
              </div>
            </label>

            <!-- API Key -->
            <label class="field field-full">
              <span class="field-label-row">
                <span class="field-label">API Key (密钥)</span>
                <span class="field-key-actions">
                  <span v-if="hasSavedKey" class="key-badge">
                    <CheckCircle2 :size="12" /> 已保存有效密钥
                  </span>
                  <button
                    v-if="hasSavedKey"
                    type="button"
                    class="btn-text-danger"
                    title="清除本地数据库中已保存的密钥"
                    @click.prevent="clearApiKey"
                  >
                    <Trash2 :size="11" />
                    <span>清空密钥</span>
                  </button>
                  <span v-else class="field-hint">本地私有存储，绝不回显明文</span>
                </span>
              </span>
              <div class="input-with-icon">
                <KeyRound :size="15" class="input-prefix-icon" />
                <input
                  v-model="form.apiKey"
                  :type="showKey ? 'text' : 'password'"
                  class="input mono with-icon with-suffix"
                  :placeholder="hasSavedKey ? '输入新密钥以覆盖保存，若不修改请留空' : 'sk-••••••••••••••••'"
                  spellcheck="false"
                  autocomplete="off"
                />
                <button
                  type="button"
                  class="icon-suffix-btn"
                  :title="showKey ? '隐藏密钥' : '显示密钥'"
                  @click="toggleKeyVisibility"
                >
                  <EyeOff v-if="showKey" :size="14" />
                  <Eye v-else :size="14" />
                </button>
              </div>
            </label>

            <!-- 模型名称与在线获取 -->
            <div class="field field-full">
              <span class="field-label-row">
                <span class="field-label">模型名称 (Model) <span class="required">*</span></span>
                <div class="model-action-group">
                  <span v-if="availableModels.length > 0" class="tag tag-neutral">
                    已拉取 {{ availableModels.length }} 个模型
                  </span>
                  <button
                    type="button"
                    class="btn btn-ghost btn-sm fetch-models-btn"
                    :disabled="fetchingModels || !isConfigured"
                    title="向服务接口发送 GET /models 获取可用模型清单"
                    @click="fetchModels"
                  >
                    <Loader2 v-if="fetchingModels" :size="13" class="spinning" />
                    <RotateCw v-else :size="13" />
                    <span>{{ fetchingModels ? '拉取中...' : '获取模型列表' }}</span>
                  </button>
                </div>
              </span>

              <div class="model-input-row">
                <!-- 已拉取到列表时只保留下拉框（单一输入点，避免两个框同时出现）；
                     未拉取时用文本框手动填写，因此任何模型标识都能录入。 -->
                <select
                  v-if="availableModels.length > 0"
                  class="select model-select"
                  :value="form.model"
                  @change="onModelPicked"
                >
                  <option v-if="!availableModels.includes(form.model)" :value="form.model" disabled>
                    {{ form.model || '请选择模型' }}
                  </option>
                  <option v-for="m in availableModels" :key="m" :value="m">{{ m }}</option>
                </select>
                <input
                  v-else
                  v-model="form.model"
                  type="text"
                  class="input mono model-input"
                  placeholder="gpt-4o, deepseek-chat, qwen2.5:7b"
                  spellcheck="false"
                />
              </div>
              <span class="field-hint">
                {{
                  availableModels.length > 0
                    ? '从已拉取的列表中点选模型；如需其他标识，可清空列表后手动填写。'
                    : '支持手动填入任何模型标识，或点击「获取模型列表」从 Base URL 端点拉取。'
                }}
              </span>
            </div>

            <!-- 超时时间 -->
            <label class="field">
              <span class="field-label">响应超时时间 (秒)</span>
              <input
                v-model.number="form.timeoutSeconds"
                type="number"
                class="input"
                min="5"
                max="600"
                step="5"
              />
            </label>
          </div>

          <!-- 连通性测试状态卡片 -->
          <div
            v-if="pingStatus"
            class="ping-result"
            :class="pingStatus.ok ? 'ping-ok' : 'ping-err'"
          >
            <component :is="pingStatus.ok ? CheckCircle2 : Zap" :size="15" class="ping-icon" />
            <div class="ping-text">
              <div class="ping-title">
                {{ pingStatus.ok ? '服务连通测试成功' : '服务连通失败' }}
                <span v-if="pingStatus.ok" class="ping-ms">({{ pingStatus.latencyMs }} ms)</span>
              </div>
              <div class="ping-desc">{{ pingStatus.message }}</div>
            </div>
          </div>

          <!-- 提示说明 -->
          <div class="security-banner">
            <ShieldCheck :size="15" class="banner-icon" />
            <div class="banner-text">
              <strong>安全保证</strong>：配置已持久化于本地 SQLite 数据库，
              在界面中永不回显明文密钥；所有模型调用采用非流式 Response 模式，严格支持工具/函数声明与调用参数解析。
            </div>
          </div>
        </div>

        <div class="card-footer">
          <button
            type="button"
            class="btn btn-ghost"
            :disabled="testing || !isConfigured"
            @click="testConnection"
          >
            <Loader2 v-if="testing" :size="14" class="spinning" />
            <Radio v-else :size="14" />
            <span>{{ testing ? '正在探测...' : '测试连通性' }}</span>
          </button>
          <div class="flex-fill" />
          <button
            type="button"
            class="btn btn-accent"
            :disabled="saving || loading"
            @click="saveSettings"
          >
            <Loader2 v-if="saving" :size="14" class="spinning" />
            <Save v-else :size="14" />
            <span>{{ saving ? '保存中...' : '保存模型配置' }}</span>
          </button>
        </div>
      </section>

      <!-- 第二部分：测试引擎选择器 -->
      <section class="card">
        <div class="card-header">
          <div class="card-title-group">
            <Blocks :size="17" class="accent-icon" />
            <div>
              <h2 class="card-title">测试执行引擎</h2>
              <p class="card-sub">
                选择执行测试计划所用的引擎。测试内容与执行引擎已解耦，切换引擎无需改动计划 JSON。
              </p>
            </div>
          </div>
          <div class="mcp-header-status">
            <span v-if="enginesLoading" class="tag tag-neutral">
              <Loader2 :size="12" class="spinning" />
              <span>探测中...</span>
            </span>
            <span v-else-if="activeEngine?.available" class="tag tag-success">
              <CheckCircle2 :size="12" />
              <span>已选引擎就绪</span>
            </span>
            <span v-else class="tag tag-danger">
              <AlertCircle :size="12" />
              <span>已选引擎不可用</span>
            </span>
          </div>
        </div>

        <div class="card-body">
          <!-- 引擎卡片列表 -->
          <div class="engine-list">
            <div
              v-for="item in engines"
              :key="item.id"
              class="engine-card"
              :class="{ 'engine-card-active': item.id === activeEngineId }"
              role="button"
              tabindex="0"
              @click="selectEngine(item.id)"
              @keydown.enter.prevent="selectEngine(item.id)"
              @keydown.space.prevent="selectEngine(item.id)"
            >
              <div class="engine-card-head">
                <span class="engine-icon" :class="item.id === 'native' ? 'icon-native' : 'icon-cli'">
                  <component :is="item.id === 'native' ? Server : Terminal" :size="15" />
                </span>
                <span class="engine-name">{{ engineMeta(item.id).name }}</span>
                <span class="engine-kind">{{ engineMeta(item.id).kind }}</span>
                <span
                  class="engine-dot"
                  :class="item.available ? 'dot-ok' : 'dot-bad'"
                  :title="item.available ? '就绪' : item.error_kind ?? '不可用'"
                />
                <!-- 每个引擎右上角都有配置入口；详情以弹窗呈现，不挤占页面高度。 -->
                <button
                  type="button"
                  class="engine-config-btn"
                  :title="`配置 ${engineMeta(item.id).name}`"
                  @click.stop="openEngineConfig(item)"
                >
                  <Settings2 :size="12" />
                </button>
              </div>
              <div class="engine-desc">{{ engineMeta(item.id).description }}</div>
              <div class="engine-meta">
                <span class="mono">{{ item.id }}</span>
                <span class="meta-sep">·</span>
                <span>{{ engineMeta(item.id).transports }}</span>
                <template v-if="item.version">
                  <span class="meta-sep">·</span>
                  <span class="mono">v{{ item.version }}</span>
                </template>
                <template v-if="!item.available && item.error_kind">
                  <span class="meta-sep">·</span>
                  <span class="engine-error">{{ item.error_kind }}</span>
                </template>
              </div>
            </div>

            <!-- 导入引擎（界面占位，未实现具体逻辑） -->
            <div
              class="engine-card engine-card-import"
              role="button"
              tabindex="0"
              @click="importEngine"
              @keydown.enter.prevent="importEngine"
            >
              <div class="engine-card-head">
                <span class="engine-icon icon-import">
                  <Download :size="15" />
                </span>
                <span class="engine-name">导入引擎</span>
                <span class="engine-kind">预览</span>
              </div>
              <div class="engine-desc">
                接入自定义 MCP 测试引擎（如远端执行器）。当前仅展示入口，导入逻辑尚未实现。
              </div>
              <div class="engine-meta">
                <span>支持 stdio / HTTP MCP 引擎</span>
              </div>
            </div>
          </div>

          <!-- 引擎详情改为弹窗，见页面底部的 AppModal -->
        </div>

        <div class="card-footer">
          <button
            type="button"
            class="btn btn-ghost"
            :disabled="enginesLoading"
            @click="refreshEngines(true)"
          >
            <Loader2 v-if="enginesLoading" :size="14" class="spinning" />
            <RefreshCw v-else :size="14" />
            <span>{{ enginesLoading ? '正在探测...' : '刷新引擎状态' }}</span>
          </button>
          <div class="flex-fill" />
          <span class="mcp-footer-hint">所选引擎会同步为「用例计划」页的默认执行引擎</span>
        </div>
      </section>


      <!-- 引擎配置弹窗：点击任意引擎右上角的齿轮打开 -->
      <AppModal
        v-model:open="configOpen"
        :title="`${configEngineName}配置`"
        :subtitle="
          configEngineId === 'postman'
            ? 'MCP 服务元信息、受控工具清单与外部客户端接入配置。保存按钮已预留，暂不写入后端配置。'
            : '本地原生引擎的运行方式与断言能力。该引擎无外部服务，保存按钮已预留。'
        "
        confirm-text="保存"
        @confirm="saveEngineConfig"
        @cancel="closeEngineConfig"
      >
        <div class="modal-engine">
          <!-- 按引擎分别渲染：弹窗展示的是「该引擎自己的」服务详情，
               而不是把 postmanmcp 的内容套给所有引擎。 -->
          <div v-if="configEngineId === 'postman'" class="mcp-server-box">
            <div class="server-box-header">
              <div class="server-box-title">
                <Terminal :size="15" class="server-icon" />
                <span class="server-name">postmanmcp 服务详情</span>
                <span class="server-badge">系统内置</span>
              </div>
              <span class="server-transport">协议：stdio (标准输入输出)</span>
            </div>

            <div class="mcp-meta-grid">
              <div class="mcp-meta-item">
                <span class="meta-label">服务标识 (ID)</span>
                <span class="meta-val mono">postmanmcp</span>
              </div>
              <div class="mcp-meta-item">
                <span class="meta-label">通信传输</span>
                <span class="meta-val">stdio (子进程标准输入输出)</span>
              </div>
              <div class="mcp-meta-item">
                <span class="meta-label">启动命令 (Command)</span>
                <span class="meta-val mono">uv run --project backend --locked api-agent-mcp</span>
              </div>
              <div class="mcp-meta-item">
                <span class="meta-label">执行基座</span>
                <span class="meta-val">受控原生 Postman CLI (Newman v2.1 报告)</span>
              </div>
            </div>

            <div class="mcp-tools-section">
              <div class="tools-section-title">
                <Wrench :size="13" />
                <span>受控工具清单 (Tools - 严格纯数据契约，无任意代码/文件/Shell 执行)</span>
              </div>
              <div class="mcp-tools-grid">
                <div class="tool-card">
                  <div class="tool-card-head">
                    <code class="tool-name">postman_status</code>
                    <span class="tool-tag tag-read">只读探活</span>
                  </div>
                  <div class="tool-desc">
                    探测原生 Postman CLI 在本地的可用性及版本号（零网络请求、安全探测）。
                  </div>
                </div>

                <div class="tool-card">
                  <div class="tool-card-head">
                    <code class="tool-name">validate_plan</code>
                    <span class="tool-tag tag-validate">结构校验</span>
                  </div>
                  <div class="tool-desc">
                    离线校验 v1 ExecutionPlan 结构自洽性与本地回环白名单（零网络开销）。
                  </div>
                </div>

                <div class="tool-card">
                  <div class="tool-card-head">
                    <code class="tool-name">run_plan</code>
                    <span class="tool-tag tag-exec">受控执行</span>
                  </div>
                  <div class="tool-desc">
                    在临时隔离沙箱中驱动 Postman CLI 执行 API 计划，产出确定性断言差异报告。
                  </div>
                </div>
              </div>
            </div>

            <div class="config-snippet">
              <div class="snippet-header">
                <span class="snippet-title">外部客户端接入配置 (Claude Desktop / Cursor / Cline)</span>
                <CopyButton :text="mcpConfigJson" label="复制配置" copied-label="已复制" />
              </div>
              <pre class="snippet-code"><code>{{ mcpConfigJson }}</code></pre>
            </div>
          </div>

          <!-- 本地原生引擎：没有外部服务，展示它自己的内建能力。 -->
          <div v-else class="mcp-server-box">
            <div class="server-box-header">
              <div class="server-box-title">
                <Server :size="15" class="server-icon" />
                <span class="server-name">本地原生引擎详情</span>
                <span class="server-badge">系统内置</span>
              </div>
              <span class="server-transport">协议：进程内执行（无外部进程）</span>
            </div>

            <div class="mcp-meta-grid">
              <div class="mcp-meta-item">
                <span class="meta-label">引擎标识 (ID)</span>
                <span class="meta-val mono">native</span>
              </div>
              <div class="mcp-meta-item">
                <span class="meta-label">运行方式</span>
                <span class="meta-val">进程内 httpx，不启动子进程</span>
              </div>
              <div class="mcp-meta-item">
                <span class="meta-label">当前版本</span>
                <span class="meta-val mono">{{ configEngineVersion || '—' }}</span>
              </div>
              <div class="mcp-meta-item">
                <span class="meta-label">外部依赖</span>
                <span class="meta-val">无（纯 Python，跨平台）</span>
              </div>
            </div>

            <div class="mcp-tools-section">
              <div class="tools-section-title">
                <Wrench :size="13" />
                <span>引擎能力（5 类受控断言，与 Postman CLI 引擎判定一致）</span>
              </div>
              <div class="mcp-tools-grid">
                <div class="tool-card">
                  <div class="tool-card-head">
                    <code class="tool-name">status</code>
                    <span class="tool-tag tag-read">状态码</span>
                  </div>
                  <div class="tool-desc">比对 HTTP 状态码是否等于期望值。</div>
                </div>
                <div class="tool-card">
                  <div class="tool-card-head">
                    <code class="tool-name">json_equals</code>
                    <span class="tool-tag tag-validate">值相等</span>
                  </div>
                  <div class="tool-desc">按 JSON Pointer 取值并做类型严格的深比较。</div>
                </div>
                <div class="tool-card">
                  <div class="tool-card-head">
                    <code class="tool-name">content_type</code>
                    <span class="tool-tag tag-exec">响应类型</span>
                  </div>
                  <div class="tool-desc">校验 Content-Type 是否为 application/json。</div>
                </div>
              </div>
            </div>

            <div class="config-snippet">
              <div class="snippet-header">
                <span class="snippet-title">说明</span>
              </div>
              <pre class="snippet-code"><code>本地原生引擎在 API-Agent 进程内直接发包，无服务端配置项。若需要沙箱隔离与外部报告，
请切换到 Postman CLI 引擎并配置其 MCP 服务。</code></pre>
            </div>
          </div>
        </div>
      </AppModal>

      <!-- 第三部分：账户与会话操作 -->
      <section class="card">
        <div class="card-header">
          <div class="card-title-group">
            <LogOut :size="17" class="danger-icon" />
            <div>
              <h2 class="card-title">会话与操作</h2>
              <p class="card-sub">
                管理当前本地客户端的会话状态。
              </p>
            </div>
          </div>
        </div>
        <div class="card-body">
          <div class="session-info-row">
            <div>
              <div class="session-label">当前运行环境</div>
              <div class="session-desc">API-Agent 单进程本地工作台（无需外部身份认证）</div>
            </div>
            <button
              type="button"
              class="btn btn-danger"
              @click="handleLogout"
            >
              <LogOut :size="14" />
              <span>退出登录</span>
            </button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
/* 设置页是长表单，必须能纵向滚动。
   此前用 `display:flex` + `height:100%`，内部内容会超出容器却不让外层滚动
   （scrollHeight 恒等于 clientHeight），导致下半部分内容不可见、按钮点不到。 */
.settings-page {
  padding: 24px 20px 64px;
  min-height: 100%;
  box-sizing: border-box;
}

.workbench-settings { display: grid; gap: 12px; }
.workbench-settings fieldset { margin: 0; min-width: 0; padding: 8px 12px 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }
.workbench-settings legend { padding-inline: 4px; font-weight: 600; }

.settings-container {
  width: 100%;
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.page-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 4px;
}

.head-icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: var(--radius-md);
  background: var(--control-gradient);
  border: 1px solid var(--border-default);
  color: var(--text-primary);
  flex: none;
}

.page-title {
  margin: 0;
  font-size: 17px;
  font-weight: 650;
  letter-spacing: -0.015em;
  color: var(--text-primary);
}

.page-desc {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--text-tertiary);
  line-height: 1.6;
}

.card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--surface-gradient);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--panel-header-gradient);
}

.card-title-group {
  display: flex;
  align-items: flex-start;
  gap: 11px;
}

.accent-icon {
  color: var(--accent);
  margin-top: 2px;
  flex: none;
}

.danger-icon {
  color: var(--danger);
  margin-top: 2px;
  flex: none;
}

.card-title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  color: var(--text-primary);
}

.card-sub {
  margin: 3px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  line-height: 1.6;
}

.card-body {
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.card-footer {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  background: var(--bg-subtle);
  border-top: 1px solid var(--border-subtle);
}

.flex-fill {
  flex: 1;
}

/* 常用预设芯片 */
.presets-section {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--bg-canvas);
}

.presets-label {
  font-size: 11.5px;
  font-weight: 550;
  color: var(--text-secondary);
}

.presets-list {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.preset-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 25px;
  padding: 0 9px;
  border: 1px solid var(--border-default);
  border-radius: 99px;
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.15s;
}

.preset-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}

.preset-tag {
  font-size: 9.5px;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--bg-subtle);
  color: var(--text-tertiary);
}

/* 表单布局 */
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.field-full {
  grid-column: 1 / -1;
}

.field-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.required {
  color: var(--danger);
}

.field-hint {
  font-size: 11px;
  color: var(--text-tertiary);
}

.key-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 500;
  color: var(--success);
  background: var(--success-soft);
  padding: 1px 7px;
  border-radius: 99px;
}

.field-key-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.btn-text-danger {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  background: transparent;
  border: 0;
  color: var(--danger);
  font-size: 11px;
  cursor: pointer;
  padding: 1px 4px;
  border-radius: 4px;
}

.btn-text-danger:hover {
  background: var(--danger-soft);
}

.model-action-group {
  display: flex;
  align-items: center;
  gap: 8px;
}

.fetch-models-btn {
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
  gap: 4px;
}

.model-input-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.model-input {
  flex: 1;
}

.model-select {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 12px;
}

.input-with-icon {
  position: relative;
  display: flex;
  align-items: center;
}

.input-prefix-icon {
  position: absolute;
  left: 10px;
  color: var(--text-tertiary);
  pointer-events: none;
}

.input.with-icon {
  padding-left: 32px;
}

.input.with-suffix {
  padding-right: 36px;
}

.icon-suffix-btn {
  position: absolute;
  right: 6px;
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
}

.icon-suffix-btn:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

/* 连通性测试卡片 */
.ping-result {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 11px 14px;
  border-radius: var(--radius-md);
  font-size: 12px;
}

.ping-ok {
  background: var(--success-soft);
  border: 1px solid var(--success-border);
  color: var(--success);
}

.ping-err {
  background: var(--danger-soft);
  border: 1px solid var(--danger-border);
  color: var(--danger);
}

.ping-icon {
  margin-top: 1px;
  flex: none;
}

.ping-title {
  font-weight: 600;
}

.ping-ms {
  font-family: var(--font-mono);
  font-weight: normal;
}

.ping-desc {
  margin-top: 2px;
  font-size: 11.5px;
  opacity: 0.9;
}

/* 安全说明条 */
.security-banner {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 10px 13px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  color: var(--text-tertiary);
  font-size: 11.5px;
  line-height: 1.6;
}

.banner-icon {
  color: var(--text-secondary);
  margin-top: 2px;
  flex: none;
}

.banner-text strong {
  color: var(--text-secondary);
}

/* 优雅原生样式的 Switch 开关 */
.switch-control {
  position: relative;
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}

.switch-label {
  font-size: 12px;
  font-weight: 550;
  color: var(--text-secondary);
}

.switch-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.switch-slider {
  position: relative;
  display: inline-block;
  width: 36px;
  height: 20px;
  background: var(--border-default);
  border-radius: 99px;
  transition: background var(--motion-normal);
}

.switch-slider::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  background: var(--switch-thumb);
  border-radius: 50%;
  box-shadow: var(--shadow-sm);
  transition: transform 0.2s;
}

.switch-input:checked + .switch-slider {
  background: var(--accent-gradient);
}

/* 保留原生复选框键盘交互，焦点投射到可见开关轨道。 */
.switch-input:focus-visible + .switch-slider {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

.switch-input:checked + .switch-slider::after {
  transform: translateX(16px);
}

/* 退出登录行 */
.session-info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.session-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.session-desc {
  font-size: 11.5px;
  color: var(--text-tertiary);
  margin-top: 2px;
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* MCP 服务部分 */
.mcp-header-status {
  display: flex;
  align-items: center;
}

/* 引擎选择卡片 */
.engine-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}

.engine-card {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 12px 14px;
  position: relative;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
}

.engine-card:hover {
  border-color: var(--accent-border);
  background: var(--bg-hover);
}

.engine-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 每个引擎右上角的配置齿轮：与状态圆点同一条视觉线上，点击不触发“选中引擎”。 */
.engine-config-btn {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  margin-left: 4px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  flex: none;
}

.engine-config-btn:hover {
  border-color: var(--border-default);
  background: var(--bg-surface);
  color: var(--text-primary);
}

.engine-card-active {
  border-color: var(--accent);
  background: var(--accent-soft);
  box-shadow: 0 0 0 1px var(--accent) inset;
}

.engine-card-active:hover {
  background: var(--accent-soft);
}

.engine-card-import {
  border-style: dashed;
  background: var(--bg-canvas);
}

.engine-card-head {
  display: flex;
  align-items: center;
  gap: 7px;
}

.engine-icon {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: var(--radius-sm);
  flex: none;
}

.icon-native {
  background: var(--success-soft);
  color: var(--success);
}

.icon-cli {
  background: var(--accent-soft);
  color: var(--accent);
}

.icon-import {
  background: var(--bg-subtle);
  color: var(--text-tertiary);
}

.engine-name {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text-primary);
}

.engine-kind {
  font-size: 9.5px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg-subtle);
  color: var(--text-tertiary);
  font-weight: 500;
}

.engine-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  margin-left: auto;
  flex: none;
}

.dot-ok {
  background: var(--success);
  box-shadow: 0 0 0 3px var(--success-soft);
}

.dot-bad {
  background: var(--danger);
  box-shadow: 0 0 0 3px var(--danger-soft);
}

.engine-desc {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.55;
}

.engine-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  font-size: 10.5px;
  color: var(--text-tertiary);
}

.meta-sep {
  opacity: 0.5;
}

.engine-error {
  color: var(--danger);
}

/* 弹窗内的引擎详情：外层已是模态卡片，这里不再套第二层圆角边框。 */
.modal-engine .mcp-server-box {
  border: 0;
  background: transparent;
  padding: 0;
}

.mcp-server-box {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
}

.server-box-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-subtle);
}

.server-box-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.server-icon {
  color: var(--accent);
}

.server-name {
  font-size: 13px;
  font-weight: 650;
  color: var(--text-primary);
}

.server-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 99px;
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 550;
}

.server-transport {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.mcp-meta-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px 16px;
  padding: 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.mcp-meta-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.meta-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.meta-val {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
}

.mcp-tools-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.tools-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-secondary);
}

.mcp-tools-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.tool-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.tool-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.tool-name {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--accent);
}

.tool-tag {
  font-size: 9.5px;
  padding: 1px 5px;
  border-radius: 4px;
  font-weight: 500;
}

.tag-read {
  background: var(--bg-subtle);
  color: var(--text-secondary);
}

.tag-validate {
  background: var(--info-soft);
  color: var(--info);
}

.tag-exec {
  background: var(--success-soft);
  color: var(--success);
}

.tool-desc {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.config-snippet {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  overflow: hidden;
}

.snippet-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  background: var(--bg-subtle);
  border-bottom: 1px solid var(--border-subtle);
}

.snippet-title {
  font-size: 11px;
  font-weight: 550;
  color: var(--text-secondary);
}

.snippet-code {
  margin: 0;
  padding: 12px;
  font-size: 11.5px;
  font-family: var(--font-mono);
  color: var(--text-primary);
  background: var(--bg-sunken);
  overflow-x: auto;
  line-height: 1.5;
}

.mcp-footer-hint {
  font-size: 11.5px;
  color: var(--text-tertiary);
}

@media (max-width: 720px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
  .mcp-meta-grid,
  .mcp-tools-grid {
    grid-template-columns: 1fr;
  }
}
</style>
