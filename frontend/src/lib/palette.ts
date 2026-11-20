/** Central light/dark color palettes and browser-local customization. */
export type PaletteTheme = 'dark' | 'light'

export const PALETTE_STORAGE_KEY = 'api-agent.palette'

export const COLOR_GROUPS = [
  { label: '界面背景', tokens: ['bg-app', 'bg-canvas', 'bg-sidebar', 'bg-surface', 'bg-rail', 'bg-subtle', 'bg-sunken', 'bg-input', 'bg-hover', 'bg-active', 'switch-thumb', 'modal-backdrop'] },
  { label: '文字与边框', tokens: ['text-primary', 'text-secondary', 'text-tertiary', 'text-inverse', 'text-on-accent', 'border-subtle', 'border-default', 'border-strong', 'border-highlight'] },
  { label: '强调色', tokens: ['accent', 'accent-hover', 'accent-soft', 'accent-border', 'action-fill', 'action-fill-hover'] },
  { label: '状态色', tokens: ['success', 'success-soft', 'success-border', 'warning', 'warning-soft', 'warning-border', 'danger', 'danger-soft', 'danger-border', 'info', 'info-soft', 'info-border'] },
  { label: '请求方法', tokens: ['method-get', 'method-post', 'method-put', 'method-patch', 'method-delete'] },
  { label: '阴影与品牌', tokens: ['shadow-color', 'brand-highlight', 'brand-start'] },
] as const

export const COLOR_LABELS: Record<string, string> = {
  'bg-app': '应用底色', 'bg-canvas': '工作画布', 'bg-sidebar': '侧栏背景', 'bg-surface': '面板背景',
  'bg-rail': '次级面板', 'bg-subtle': '控件背景', 'bg-sunken': '内嵌背景', 'bg-input': '输入框背景',
  'bg-hover': '悬停背景', 'bg-active': '选中背景', 'text-primary': '主要文字', 'text-secondary': '次要文字',
  'text-tertiary': '辅助文字', 'text-inverse': '反色文字', 'text-on-accent': '强调色上文字',
  'border-subtle': '弱边框', 'border-default': '标准边框', 'border-strong': '强调边框',
  accent: '强调色', 'accent-hover': '强调色悬停', 'accent-soft': '强调色浅底', 'accent-border': '强调色边框',
  'action-fill': '主要操作', 'action-fill-hover': '主要操作悬停',
  success: '成功文字', 'success-soft': '成功浅底', 'success-border': '成功边框',
  warning: '警告文字', 'warning-soft': '警告浅底', 'warning-border': '警告边框',
  danger: '错误文字', 'danger-soft': '错误浅底', 'danger-border': '错误边框',
  info: '信息文字', 'info-soft': '信息浅底', 'info-border': '信息边框',
  'method-get': 'GET', 'method-post': 'POST', 'method-put': 'PUT', 'method-patch': 'PATCH', 'method-delete': 'DELETE',
  'shadow-color': '阴影', 'border-highlight': '高亮边框',
  'brand-highlight': '品牌高光', 'brand-start': '品牌渐变起点', 'switch-thumb': '开关滑块', 'modal-backdrop': '弹窗遮罩',
}

export const DEFAULT_PALETTES: Record<PaletteTheme, Record<string, string>> = {
  dark: {
    'bg-app': '#111113', 'bg-canvas': '#19191d', 'bg-sidebar': '#121215', 'bg-surface': '#19191d',
    'bg-rail': '#151518', 'bg-subtle': '#222226', 'bg-sunken': '#141417', 'bg-input': '#17171b',
    'bg-hover': '#25252b', 'bg-active': '#2d2d35', 'text-primary': '#f1f0f5', 'text-secondary': '#bbb9c6',
    'text-tertiary': '#9795a6', 'text-inverse': '#15151a', 'text-on-accent': '#ffffff', 'switch-thumb': '#ffffff',
    'border-subtle': '#2a2a31', 'border-default': '#383841', 'border-strong': '#555464',
    accent: '#b6adff', 'accent-hover': '#ccc5ff', 'accent-soft': '#292439', 'accent-border': '#50446f',
    'action-fill': '#5b50b9', 'action-fill-hover': '#665ac5', success: '#83d5a8', 'success-soft': '#1d382b',
    'success-border': '#315b42', warning: '#e7c17b', 'warning-soft': '#392f20', 'warning-border': '#5d4b2d',
    danger: '#f09a9a', 'danger-soft': '#3b2529', 'danger-border': '#633b40', info: '#9bc9f2',
    'info-soft': '#203447', 'info-border': '#38546e', 'method-get': '#83d5a8', 'method-post': '#e7c17b',
    'method-put': '#9bc9f2', 'method-patch': '#c5afe8', 'method-delete': '#f09a9a',
    'shadow-color': '#000000', 'border-highlight': '#8374e3', 'brand-highlight': '#5b50b9',
    'brand-start': '#b5a9f5', 'modal-backdrop': '#000000',
  },
  light: {
    'bg-app': '#f1f1f4', 'bg-canvas': '#fafafd', 'bg-sidebar': '#f1f1f4', 'bg-surface': '#ffffff',
    'bg-rail': '#f8f8fb', 'bg-subtle': '#f4f4f8', 'bg-sunken': '#f8f8fb', 'bg-input': '#ffffff',
    'bg-hover': '#ececf2', 'bg-active': '#e4e4ed', 'text-primary': '#212129', 'text-secondary': '#555365',
    'text-tertiary': '#6e6a7f', 'text-inverse': '#ffffff', 'text-on-accent': '#ffffff', 'switch-thumb': '#ffffff',
    'border-subtle': '#e6e5ec', 'border-default': '#d8d7e2', 'border-strong': '#aaa7bd',
    accent: '#5144b3', 'accent-hover': '#40349a', 'accent-soft': '#eeebff', 'accent-border': '#d5cff4',
    'action-fill': '#5749bb', 'action-fill-hover': '#493ca9', success: '#18734f', 'success-soft': '#eaf7ef',
    'success-border': '#c5e7d3', warning: '#94601e', 'warning-soft': '#fff5e3', 'warning-border': '#efd9b2',
    danger: '#bd3945', 'danger-soft': '#fff0f0', 'danger-border': '#efcdd1', info: '#2867ac',
    'info-soft': '#edf4ff', 'info-border': '#ccdef5', 'method-get': '#18734f', 'method-post': '#94601e',
    'method-put': '#2867ac', 'method-patch': '#7954ae', 'method-delete': '#bd3945',
    'shadow-color': '#1e1a34', 'border-highlight': '#7465d6', 'brand-highlight': '#5749bb',
    'brand-start': '#b6acf0', 'modal-backdrop': '#181526',
  },
}

/** Read validated user overrides without modifying the canonical defaults. */
export function readPaletteOverrides(): Partial<Record<PaletteTheme, Record<string, string>>> {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(PALETTE_STORAGE_KEY) ?? '{}')
    if (!parsed || typeof parsed !== 'object') return {}
    const source = parsed as Record<string, unknown>
    return Object.fromEntries((['dark', 'light'] as const).map(theme => {
      const values = source[theme]
      if (!values || typeof values !== 'object') return [theme, {}]
      const valid = Object.fromEntries(Object.entries(values).filter(([key, value]) =>
        Object.hasOwn(DEFAULT_PALETTES[theme], key) && typeof value === 'string' && /^#[\da-f]{6}$/i.test(value),
      ))
      return [theme, valid]
    }))
  } catch {
    return {}
  }
}

/** Return a fresh merged palette so controls cannot mutate frozen defaults. */
export function getPalette(theme: PaletteTheme): Record<string, string> {
  return { ...DEFAULT_PALETTES[theme], ...readPaletteOverrides()[theme] }
}

/** Apply every shared token to the root so all components update together. */
export function applyPalette(theme: PaletteTheme, palette = getPalette(theme)): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  for (const [token, value] of Object.entries(palette)) root.style.setProperty(`--palette-${token}`, value)
}

/** Persist one theme's customization; other theme values and defaults remain untouched. */
export function savePalette(theme: PaletteTheme, palette: Record<string, string>): void {
  const all = readPaletteOverrides()
  const valid = Object.fromEntries(Object.entries(palette).filter(([key, value]) =>
    Object.hasOwn(DEFAULT_PALETTES[theme], key) && /^#[\da-f]{6}$/i.test(value),
  ))
  all[theme] = valid
  try { window.localStorage.setItem(PALETTE_STORAGE_KEY, JSON.stringify(all)) } catch { /* Current colors remain active for this session. */ }
  if (document.documentElement.dataset.theme === theme) applyPalette(theme, palette)
}

/** Reset only the active theme to the canonical palette. */
export function resetPalette(theme: PaletteTheme): void {
  const all = readPaletteOverrides()
  delete all[theme]
  try { window.localStorage.setItem(PALETTE_STORAGE_KEY, JSON.stringify(all)) } catch { /* Runtime defaults still apply. */ }
  if (document.documentElement.dataset.theme === theme) applyPalette(theme, DEFAULT_PALETTES[theme])
}
