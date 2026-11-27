/** 外观偏好和真实侧栏动作的回归，浏览器几何位置另通过页面验收。 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')
const { compile } = require('@vue/compiler-dom')
const { renderToString } = require('@vue/server-renderer')

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  }
}

function loadCopy() {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/copy.ts'), 'utf8')
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  const exports = {}
  new Function('exports', js)(exports)
  return exports
}

function loadAppearance(storage) {
  const properties = new Map()
  const document = { documentElement: { dataset: {}, style: { setProperty: (key, value) => properties.set(key, value), getPropertyValue: key => properties.get(key) ?? '', removeProperty() {} } } }
  const paletteSource = fs.readFileSync(path.join(__dirname, '../src/lib/palette.ts'), 'utf8')
  const paletteJs = ts.transpileModule(paletteSource, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const paletteExports = {}
  new Function('exports', 'window', 'document', paletteJs)(paletteExports, { localStorage: storage }, document)
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/appearance.ts'), 'utf8')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const result = {}
  new Function('exports', 'window', 'document', 'require', js)(result, { localStorage: storage }, document, () => paletteExports)
  return { ...paletteExports, ...result, document, copy: loadCopy() }
}

test('theme and collapsed preference survive reload and reject unknown persisted values', () => {
  const storage = memoryStorage()
  let api = loadAppearance(storage)
  assert.equal(api.readTheme(), 'dark')
  assert.equal(api.readSidebarCollapsed(), false)
  api.writeTheme('light')
  api.writeSidebarCollapsed(true)
  api = loadAppearance(storage)
  api.initializeAppearance()
  assert.equal(api.readTheme(), 'light')
  assert.equal(api.document.documentElement.dataset.theme, 'light')
  assert.equal(api.readSidebarCollapsed(), true)
  storage.setItem(api.THEME_STORAGE_KEY, 'invalid')
  storage.setItem(api.SIDEBAR_STORAGE_KEY, 'yes')
  assert.equal(api.readTheme(), 'dark')
  assert.equal(api.readSidebarCollapsed(), false)
})

test('blocked browser storage still permits changing the document theme', () => {
  const storage = {
    getItem() { throw new Error('Storage denied') },
    setItem() { throw new Error('Storage denied') },
  }
  const api = loadAppearance(storage)
  assert.equal(api.readTheme(), 'dark')
  assert.equal(api.readSidebarCollapsed(), false)
  assert.doesNotThrow(() => api.writeTheme('light'))
  assert.doesNotThrow(() => api.writeSidebarCollapsed(true))
  api.applyTheme('light')
  assert.equal(api.document.documentElement.dataset.theme, 'light')
})

test('theme switching uses a view transition when supported', () => {
  const storage = memoryStorage()
  let updated = false
  const api = loadAppearance(storage)
  api.document.documentElement.dataset.theme = 'dark'
  api.document.startViewTransition = callback => { updated = true; callback(); return { finished: Promise.resolve() } }
  api.transitionTheme('light')
  assert.equal(updated, true)
  assert.equal(api.document.documentElement.dataset.theme, 'light')
})

test('theme and sidebar transitions explicitly retain motion under reduced-motion preferences', () => {
  const tokens = fs.readFileSync(path.join(__dirname, '../src/styles/tokens.css'), 'utf8')
  const base = fs.readFileSync(path.join(__dirname, '../src/styles/base.css'), 'utf8')
  const sidebar = fs.readFileSync(path.join(__dirname, '../src/components/AppSidebar.vue'), 'utf8')
  assert.match(tokens, /prefers-reduced-motion: reduce[\s\S]*?transition-duration: 0\.01ms/)
  assert.match(base, /prefers-reduced-motion: reduce[\s\S]*?view-transition-group\(root\)[\s\S]*?760ms !important/)
  assert.match(sidebar, /prefers-reduced-motion: reduce[\s\S]*?\.sidebar[\s\S]*?transition-duration: 560ms !important/)
  assert.match(sidebar, /transition: width 560ms cubic-bezier/)
})

function loadSidebar(storage = memoryStorage()) {
  const source = fs.readFileSync(path.join(__dirname, '../src/components/AppSidebar.vue'), 'utf8')
  const script = source.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/g, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const appearance = loadAppearance(storage)
  const route = vue.reactive({ path: '/testcases' })
  const icon = { render: () => vue.h('svg', { class: 'test-icon' }) }
  const icons = Object.fromEntries([
    'Boxes', 'Braces', 'Cable', 'Cpu', 'FolderKanban', 'History', 'ListTree', 'Moon', 'PanelLeftClose',
    'PanelLeftOpen', 'Plus', 'SettingsIcon', 'Sun', 'Terminal',
  ].map(name => [name, icon]))
  const ProjectLogo = {
    props: ['size'],
    render: () => vue.h('span', { class: 'signal-logo' }),
  }
  const RouterLink = {
    props: ['to'],
    setup(props, context) {
      return () => vue.h('a', { ...context.attrs, href: props.to }, context.slots.default?.())
    },
  }
  const env = { ...vue, ...appearance, ...appearance.copy, ...icons, ProjectLogo, useRoute: () => route, RouterLink }
  const scope = vue.effectScope()
  const state = scope.run(() => new Function(...Object.keys(env),
    `${js};return {groups,theme,collapsed,isActive,toggleTheme,toggleCollapsed}`)(...Object.values(env)))
  const template = source.split('<template>')[1].split('</template>')[0]
  const render = new Function('Vue', compile(template, { mode: 'function' }).code)(vue)
  return {
    ...state, route, storage, appearance,
    render: () => {
      const context = vue.proxyRefs({ ...env, ...state })
      const app = vue.createSSRApp({ render: () => render(context, []) })
      app.component('RouterLink', RouterLink)
      app.component('ProjectLogo', ProjectLogo)
      for (const [name, value] of Object.entries(icons)) app.component(name, value)
      return renderToString(app)
    },
    dispose: () => scope.stop(),
  }
}

test('theme and logo collapse controls do not navigate, remove separate collapse button and show project tooltip bubble', async () => {
  const sidebar = loadSidebar()
  try {
    const expanded = await sidebar.render()
    assert.match(expanded, /aria-label="切换亮色主题"/)
    assert.match(expanded, /class="brand-trigger"[^>]*aria-expanded="true"|aria-expanded="true"[^>]*class="brand-trigger"/)
    assert.match(expanded, /class="brand-tooltip"[^>]*role="tooltip"/)
    assert.match(expanded, /API-Agent · API 工作台/)
    assert.ok(!expanded.includes('sidebar-toggle'), 'separate hide sidebar button should be removed')
    assert.ok(!expanded.includes('brand-text'), 'adjacent project name beside logo should be removed')
    assert.match(expanded, /<a\b[^>]*href="\/testcases"[^>]*aria-current="page"|<a\b[^>]*aria-current="page"[^>]*href="\/testcases"/)
    assert.equal([...expanded.matchAll(/aria-current="page"/g)].length, 1)
    sidebar.toggleCollapsed()
    sidebar.toggleTheme()
    await vue.nextTick()
    const collapsed = await sidebar.render()
    assert.equal(sidebar.route.path, '/testcases')
    assert.equal(sidebar.storage.getItem('api-agent.theme'), 'light')
    assert.equal(sidebar.storage.getItem('api-agent.sidebar-collapsed'), 'true')
    assert.equal(sidebar.appearance.document.documentElement.dataset.theme, 'light')
    assert.match(collapsed, /sidebar-collapsed/)
    assert.match(collapsed, /aria-expanded="false"/)
    assert.match(collapsed, /aria-label="切换暗色主题"/)
    const links = /href="([^"]+)"/g
    assert.deepEqual([...collapsed.matchAll(links)].map(match => match[1]),
      [...expanded.matchAll(links)].map(match => match[1]))
    assert.ok(!collapsed.includes('href="/executions"'))
    assert.ok(!collapsed.includes('href="/projects"'))
    for (const label of ['接口测试', '长连接测试', '智能群测', '大模型测试', '执行结果', '系统设置']) {
      assert.ok(collapsed.includes(`title="${label}"`))
      assert.ok(collapsed.includes(`aria-label="${label}"`))
    }
    assert.match(collapsed, /API 工作台/)
    assert.match(collapsed, /点击展开侧边栏/)
    assert.match(collapsed, /单点测试/)
  } finally {
    sidebar.dispose()
  }
})

test('project logo uses continuous signal transmission animation and customizable logo/assistant palette defaults', () => {
  const logoSource = fs.readFileSync(path.join(__dirname, '../src/components/ProjectLogo.vue'), 'utf8')
  assert.match(logoSource, /@keyframes signal-ring-emit/)
  assert.match(logoSource, /@keyframes signal-arc-wave/)
  assert.match(logoSource, /@keyframes signal-core-pulse/)
  assert.match(logoSource, /var\(--logo-color/)

  const storage = memoryStorage()
  const api = loadAppearance(storage)
  assert.equal(api.DEFAULT_PALETTES.dark['assistant-color'], '#22c55e')
  assert.equal(api.DEFAULT_PALETTES.light['assistant-color'], '#16a34a')
  assert.ok(api.COLOR_LABELS['logo-color'])
  assert.ok(api.COLOR_LABELS['assistant-color'])
  api.document.documentElement.dataset.theme = 'dark'
  api.savePalette('dark', { ...api.DEFAULT_PALETTES.dark, 'logo-color': '#FF6600', 'assistant-color': '#00AABB' })
  assert.equal(api.document.documentElement.style.getPropertyValue('--palette-logo-color'), '#FF6600')
  assert.equal(api.document.documentElement.style.getPropertyValue('--palette-assistant-color'), '#00AABB')
  api.resetPalette('dark')
  assert.equal(api.document.documentElement.style.getPropertyValue('--palette-logo-color'), api.DEFAULT_PALETTES.dark['logo-color'])
  assert.equal(api.document.documentElement.style.getPropertyValue('--palette-assistant-color'), api.DEFAULT_PALETTES.dark['assistant-color'])
})

test('early head script restores the same theme default before stylesheet loading', () => {
  const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  for (const [stored, expected] of [[null, 'dark'], ['dark', 'dark'], ['light', 'light'], ['wrong', 'dark']]) {
    const document = { documentElement: { dataset: {} } }
    new Function('localStorage', 'document', script)({ getItem: () => stored }, document)
    assert.equal(document.documentElement.dataset.theme, expected)
  }
})

/** 计算两种界面关键文字配色的相对亮度对比。 */
function contrastRatio(foreground, background) {
  function luminance(hex) {
    const channels = hex.slice(1).match(/../g).map(value => parseInt(value, 16) / 255)
    const [red, green, blue] = channels.map(value => value <= 0.04045
      ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue
  }
  const levels = [luminance(foreground), luminance(background)].sort((first, second) => second - first)
  return (levels[0] + 0.05) / (levels[1] + 0.05)
}

test('both palettes keep primary actions, secondary text and active tabs readable', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/palette.ts'), 'utf8')
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  const palette = {}
  new Function('exports', 'window', 'document', js)(palette, { localStorage: memoryStorage() }, { documentElement: { dataset: {} } })
  for (const [name, colors] of Object.entries(palette.DEFAULT_PALETTES)) {
    for (const [foreground, background] of [
      ['text-on-accent', 'action-fill'],
      ['text-on-accent', 'action-fill-hover'],
      ['text-tertiary', 'bg-surface'],
      ['text-secondary', 'bg-sidebar'],
      ['accent', 'bg-surface'],
    ]) {
      assert.ok(colors[foreground] && colors[background], `${name}: missing ${foreground} or ${background}`)
      assert.ok(contrastRatio(colors[foreground], colors[background]) >= 4.5,
        `${name}: ${foreground} on ${background} has insufficient contrast`)
    }
  }
})

test('palette overrides are validated, isolated by theme, applied live and reset independently', () => {
  const storage = memoryStorage()
  const api = loadAppearance(storage)
  const customDark = { ...api.DEFAULT_PALETTES.dark, accent: '#123456' }
  api.document.documentElement.dataset.theme = 'dark'
  api.savePalette('dark', customDark)
  assert.equal(api.document.documentElement.style.getPropertyValue('--palette-accent'), '#123456')
  api.savePalette('light', { ...api.DEFAULT_PALETTES.light, accent: 'red' })
  assert.equal(api.getPalette('light').accent, api.DEFAULT_PALETTES.light.accent)
  assert.equal(api.getPalette('dark').accent, '#123456')
  api.resetPalette('light')
  assert.equal(api.getPalette('dark').accent, '#123456')
  api.resetPalette('dark')
  assert.equal(api.getPalette('dark').accent, api.DEFAULT_PALETTES.dark.accent)
})
