/** 持久化控制台外观偏好，并在挂载 Vue 前同步根节点属性；禁用存储时仍允许切换。 */
export type AppearanceTheme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'api-agent.theme'
export const SIDEBAR_STORAGE_KEY = 'api-agent.sidebar-collapsed'
export const DEFAULT_THEME: AppearanceTheme = 'dark'

/** 读取单项偏好，浏览器禁用存储或读操作失败时回退为空。 */
function readPreference(key: string): string | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

/** 写入偏好，失败时仍保留组件当前内存状态。 */
function writePreference(key: string, value: string): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // 隐私模式可能拒绝写入，用户仍可在当前页面正常切换。
  }
}

/** 读取安全的主题值，未知值视为默认暗色。 */
export function readTheme(): AppearanceTheme {
  const value = readPreference(THEME_STORAGE_KEY)
  return value === 'light' || value === 'dark' ? value : DEFAULT_THEME
}

/** 读取侧栏折叠偏好，坏值和存储异常都回退为展开。 */
export function readSidebarCollapsed(): boolean {
  return readPreference(SIDEBAR_STORAGE_KEY) === 'true'
}

/** 保存主题偏好，存储不可用时保持当前内存状态。 */
export function writeTheme(theme: AppearanceTheme): void {
  writePreference(THEME_STORAGE_KEY, theme)
}

/** 保存侧栏偏好，失败不影响当前交互。 */
export function writeSidebarCollapsed(collapsed: boolean): void {
  writePreference(SIDEBAR_STORAGE_KEY, String(collapsed))
}

/** 将主题同步到 html，所有共享组件读取同一组设计令牌。 */
export function applyTheme(theme: AppearanceTheme): void {
  if (typeof document !== 'undefined') document.documentElement.dataset.theme = theme
}

/** 入口页在创建 Vue 应用前执行，与 HTML 预加载逻辑保持相同默认值。 */
export function initializeAppearance(): void {
  applyTheme(readTheme())
}
