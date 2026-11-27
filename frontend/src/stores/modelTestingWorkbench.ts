/** 大模型测试工作台的跨页内存状态仓库：保留目标配置、临时密钥（仅内存，绝不落盘）、已拉取模型、活跃探测句柄与只读回放上下文。 */
import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type {
  ModelCategory,
  ModelDiscoverResult,
  ModelProfile,
  ModelProtocol,
  ModelTestRunDetail,
  ModelTestRunListItem,
  ModelTestSubCaseSnapshot,
  ProbeCheckName,
} from '@/api/types'

export type AvailabilityFilter = 'all' | 'available' | 'unavailable' | 'unsupported' | 'untested'
export type ToolFilter = 'all' | 'supported' | 'partial' | 'unsupported' | 'untested'
export type InputModalityFilter = 'all' | 'multimodal' | 'image' | 'image_probed' | 'audio' | 'video' | 'text_only'
export type OutputModalityFilter = 'all' | 'reasoning' | 'text' | 'image' | 'audio' | 'embedding'

/** 点击 AI 回复中的子用例链接后进入的只读回放快照。 */
export interface ModelTestingReadonlyReplay {
  historyId: string
  caseId: string
  round: number
  label: string
  modelId: string
  status: string
  subCase: ModelTestSubCaseSnapshot | null
}

/** 运行中多轮/单轮模型探测任务的实时进度视图。 */
export interface ModelTestingLiveProgress {
  stage: 'idle' | 'executing' | 'cooling_down' | 'finished'
  currentRound: number
  totalRounds: number
  completedRounds: number
  cooldownMs: number
  cooldownRemainingMs: number | null
  currentModels: string[]
  completedCases: number
  totalCases: number
}

function defaultChecks(): Record<ProbeCheckName, boolean> {
  return {
    availability: true,
    tool_calling: true,
    input_modalities: true,
    output_modalities: false,
    streaming: false,
    structured_output: false,
  }
}

function defaultLiveProgress(): ModelTestingLiveProgress {
  return {
    stage: 'idle',
    currentRound: 0,
    totalRounds: 1,
    completedRounds: 0,
    cooldownMs: 500,
    cooldownRemainingMs: null,
    currentModels: [],
    completedCases: 0,
    totalCases: 0,
  }
}

/**
 * 大模型测试工作台内存状态：
 * - 切页后完整保留 Base URL、协议、仅内存临时 API Key、已发现模型列表、探测矩阵与运行句柄；
 * - 严禁将临时 API Key 写入任何浏览器持久存储（localStorage / sessionStorage）。
 */
export const useModelTestingWorkbenchStore = defineStore('modelTestingWorkbench', () => {
  const baseUrl = ref('')
  const urlPrefix = ref('')
  /** 仅驻留内存的临时 API Key，绝不写入任何持久化存储。 */
  const apiKey = ref('')
  const useSavedKey = ref(false)
  const protocol = ref<ModelProtocol>('auto')
  const timeoutSeconds = ref(25)
  const checks = reactive<Record<ProbeCheckName, boolean>>(defaultChecks())

  const discoverMeta = ref<Omit<ModelDiscoverResult, 'models'> | null>(null)
  const models = ref<ModelProfile[]>([])
  const selectedIds = ref<string[]>([])
  const activeModelId = ref<string | null>(null)

  const searchQuery = ref('')
  const categoryFilter = ref<'all' | ModelCategory>('all')
  const availabilityFilter = ref<AvailabilityFilter>('all')
  const toolFilter = ref<ToolFilter>('all')
  const inputModalityFilter = ref<InputModalityFilter>('all')
  const outputModalityFilter = ref<OutputModalityFilter>('all')

  const history = ref<ModelTestRunListItem[]>([])
  const activeRunId = ref<string | null>(null)
  const activeHistoryId = ref<string | null>(null)
  const currentRun = ref<ModelTestRunDetail | null>(null)
  const liveProgress = reactive<ModelTestingLiveProgress>(defaultLiveProgress())
  const readonlyReplay = ref<ModelTestingReadonlyReplay | null>(null)
  const draftVersion = ref(0)

  function resetLiveProgress(): void {
    Object.assign(liveProgress, defaultLiveProgress())
  }

  function resetFilters(): void {
    searchQuery.value = ''
    categoryFilter.value = 'all'
    availabilityFilter.value = 'all'
    toolFilter.value = 'all'
    inputModalityFilter.value = 'all'
    outputModalityFilter.value = 'all'
  }

  function enterReadonlyReplay(payload: ModelTestingReadonlyReplay): void {
    readonlyReplay.value = { ...payload }
  }

  function exitReadonlyReplay(): void {
    readonlyReplay.value = null
  }

  /**
   * 显式「再次测试」或「新建测试」复位：
   * - 解除只读回放锁、清空当前历史/运行绑定与展示结果并递增 draftVersion 开启全新空白对话；
   * - `keepConfig: true` 时保留当前连接与探测项配置（供再次测试），`false` 时一并清空表单。
   */
  function resetForNewTest(options: { keepConfig?: boolean } = {}): void {
    readonlyReplay.value = null
    activeRunId.value = null
    activeHistoryId.value = null
    currentRun.value = null
    resetLiveProgress()
    discoverMeta.value = null
    models.value = []
    selectedIds.value = []
    activeModelId.value = null
    resetFilters()
    if (!options.keepConfig) {
      urlPrefix.value = ''
      baseUrl.value = ''
      apiKey.value = ''
      useSavedKey.value = false
      protocol.value = 'auto'
      timeoutSeconds.value = 25
      Object.assign(checks, defaultChecks())
    }
    draftVersion.value += 1
  }

  return {
    baseUrl,
    urlPrefix,
    apiKey,
    useSavedKey,
    protocol,
    timeoutSeconds,
    checks,
    discoverMeta,
    models,
    selectedIds,
    activeModelId,
    searchQuery,
    categoryFilter,
    availabilityFilter,
    toolFilter,
    inputModalityFilter,
    outputModalityFilter,
    history,
    activeRunId,
    activeHistoryId,
    currentRun,
    liveProgress,
    readonlyReplay,
    draftVersion,
    resetLiveProgress,
    resetFilters,
    enterReadonlyReplay,
    exitReadonlyReplay,
    resetForNewTest,
  }
})
