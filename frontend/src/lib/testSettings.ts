/** 四个测试页共用的浏览器级配置，按测试类型保存，供下一次执行与 AI 工具调用读取。 */
import { reactive, watch } from 'vue'

export const TEST_SETTINGS_KEY = 'api-agent.test-settings'
export const MODEL_TESTING_SETTINGS_KEY = 'api-agent.settings.model_testing'
export const DEFAULT_COOLDOWN_MS = 500

/** Bounds and labels shared by persistence and both settings locations. */
export type TestNumberField = {
  readonly key: string
  readonly label: string
  readonly initial: number
  readonly min: number
  readonly max: number
  readonly step?: number
}

const COOLDOWN_FIELD = {
  key: 'cooldownMs',
  label: '每轮冷却 (ms)',
  initial: DEFAULT_COOLDOWN_MS,
  min: 0,
  max: 60000,
  step: 100,
} as const satisfies TestNumberField

export const testNumberFields = {
  console: [
    { key: 'sseDurationMs', label: 'SSE 观察时长（毫秒）', initial: 30000, min: 100, max: 30000 },
    { key: 'sseMaxEvents', label: 'SSE 最多事件', initial: 50, min: 1, max: 1000 },
    { key: 'timeoutMs', label: '超时（毫秒）', initial: 60000, min: 100, max: 60000 },
    COOLDOWN_FIELD,
  ],
  websocket: [
    { key: 'sendIntervalMs', label: '发送间隔（毫秒）', initial: 500, min: 0, max: 60000 },
    { key: 'maxEvents', label: '最多事件', initial: 200, min: 1, max: 2000 },
    { key: 'connectTimeoutMs', label: '连接超时（毫秒）', initial: 60000, min: 100, max: 60000 },
    { key: 'maxDurationMs', label: '最长时长（毫秒）', initial: 900000, min: 1000, max: 900000 },
    { key: 'pingIntervalMs', label: '心跳间隔（毫秒）', initial: 20000, min: 0, max: 300000 },
    COOLDOWN_FIELD,
  ],
  task: [
    { key: 'testsPerUrl', label: '每个接口生成 test 数（test/url）', initial: 3, min: 1, max: 10 },
    { key: 'observationMs', label: '流观察时长（毫秒）', initial: 30000, min: 1000, max: 30000 },
    { key: 'wallTimeSeconds', label: '任务总时长（秒）', initial: 3600, min: 10, max: 3600 },
    COOLDOWN_FIELD,
  ],
  model_testing: [
    { key: 'timeoutSeconds', label: '单模型超时（秒）', initial: 20, min: 1, max: 300, step: 1 },
    { key: 'concurrency', label: '探测并发数', initial: 4, min: 1, max: 16, step: 1 },
    COOLDOWN_FIELD,
  ],
} as const satisfies Record<string, readonly TestNumberField[]>

export type TestSettingsGroup = keyof typeof testNumberFields

/** 缺值、损坏的 JSON 或禁用存储时，交由调用方使用默认值。 */
function readSetting(key: string): unknown {
  try {
    return JSON.parse(window.localStorage.getItem(key) ?? 'null')
  } catch {
    return null
  }
}

/** 只保存调试偏好；浏览器拒绝写入时保留已修改的内存值。 */
function writeSettings(value: { model_testing: Record<string, unknown> }): void {
  try {
    window.localStorage.setItem(TEST_SETTINGS_KEY, JSON.stringify(value))
    window.localStorage.setItem(MODEL_TESTING_SETTINGS_KEY, JSON.stringify(value.model_testing))
  } catch {
    return
  }
}

/** 加载时只采用有效的已保存值；其它字段不受单项坏值影响。 */
function readNumbers<Key extends string>(fields: readonly (TestNumberField & { key: Key })[], stored: Record<string, unknown>) {
  const values = {} as Record<Key, number>
  for (const { key, initial, min, max } of fields) {
    const value = stored[key]
    values[key] = typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max ? value : initial
  }
  return values
}

function settingFields(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function defaultSettings() {
  return {
    console: {
      ...readNumbers(testNumberFields.console, {}),
      followRedirects: false,
    },
    websocket: readNumbers(testNumberFields.websocket, {}),
    task: {
      allowWrites: true,
      ...readNumbers(testNumberFields.task, {}),
    },
    model_testing: readNumbers(testNumberFields.model_testing, {}),
  }
}

/** 接续旧版接口配置、发送间隔与独立大模型测试配置，已保存的布尔设置优先于默认值。 */
function readSettings() {
  const stored = settingFields(readSetting(TEST_SETTINGS_KEY))
  const http = settingFields(stored.console ?? readSetting('api-agent.console.settings'))
  const websocket = settingFields(stored.websocket)
  const task = settingFields(stored.task)
  const modelTesting = {
    ...settingFields(stored.model_testing),
    ...settingFields(readSetting(MODEL_TESTING_SETTINGS_KEY)),
  }
  return {
    console: {
      ...readNumbers(testNumberFields.console, http),
      followRedirects: http.followRedirects === true,
    },
    websocket: readNumbers(testNumberFields.websocket, {
      ...websocket,
      sendIntervalMs: websocket.sendIntervalMs ?? readSetting('api-agent.websocket.send-interval-ms'),
    }),
    task: {
      allowWrites: typeof task.allowWrites === 'boolean' ? task.allowWrites : true,
      ...readNumbers(testNumberFields.task, task),
    },
    model_testing: readNumbers(testNumberFields.model_testing, modelTesting),
  }
}

export const testSettings = reactive(readSettings())
export const {
  console: consoleSettings,
  websocket: webSocketSettings,
  task: taskSettings,
  model_testing: modelTestingSettings,
} = testSettings

let applyingStoredSettings = false
watch(testSettings, value => {
  if (!applyingStoredSettings) writeSettings(value)
}, { flush: 'sync' })

/** 重置全部四类测试设置到默认值并清除对应的本地持久化键。 */
export function resetTestSettings(): void {
  const defaults = defaultSettings()
  applyingStoredSettings = true
  Object.assign(consoleSettings, defaults.console)
  Object.assign(webSocketSettings, defaults.websocket)
  Object.assign(taskSettings, defaults.task)
  Object.assign(modelTestingSettings, defaults.model_testing)
  applyingStoredSettings = false
  try {
    window.localStorage.removeItem(TEST_SETTINGS_KEY)
    window.localStorage.removeItem(MODEL_TESTING_SETTINGS_KEY)
    window.localStorage.removeItem('api-agent.console.settings')
    window.localStorage.removeItem('api-agent.websocket.send-interval-ms')
  } catch {
    return
  }
}

/** Synchronize open tabs without writing partial state back during a storage update. */
window.addEventListener('storage', event => {
  if (event.key !== TEST_SETTINGS_KEY && event.key !== MODEL_TESTING_SETTINGS_KEY && event.key !== null) return
  if (event.storageArea !== window.localStorage) return
  const latest = readSettings()
  applyingStoredSettings = true
  Object.assign(consoleSettings, latest.console)
  Object.assign(webSocketSettings, latest.websocket)
  Object.assign(taskSettings, latest.task)
  Object.assign(modelTestingSettings, latest.model_testing)
  applyingStoredSettings = false
})

/** 空值恢复原设置，越界吸附到最近边界，小数四舍五入到整数。 */
export function normalizeTestNumber(raw: number, previous: number, min: number, max: number) {
  if (!Number.isFinite(raw)) return { value: previous, adjustment: 'empty' }
  const value = Math.min(max, Math.max(min, Math.round(raw)))
  return { value, adjustment: raw < min || raw > max ? 'range' : value !== raw ? 'round' : null }
}
