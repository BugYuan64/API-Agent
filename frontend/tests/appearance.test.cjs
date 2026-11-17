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

function loadAppearance(storage) {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/appearance.ts'), 'utf8')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const result = {}
  const document = { documentElement: { dataset: {} } }
  new Function('exports', 'window', 'document', js)(result, { localStorage: storage }, document)
  return { ...result, document }
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

function loadSidebar(storage = memoryStorage()) {
  const source = fs.readFileSync(path.join(__dirname, '../src/components/AppSidebar.vue'), 'utf8')
  const script = source.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/g, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const appearance = loadAppearance(storage)
  const route = vue.reactive({ path: '/projects' })
  const icon = { render: () => vue.h('svg', { class: 'test-icon' }) }
  const icons = Object.fromEntries([
    'Braces', 'Cable', 'FolderKanban', 'History', 'ListTree', 'Moon', 'PanelLeftClose',
    'PanelLeftOpen', 'Plus', 'SettingsIcon', 'Sun', 'Terminal',
  ].map(name => [name, icon]))
  const RouterLink = {
    props: ['to'],
    setup(props, context) {
      return () => vue.h('a', { ...context.attrs, href: props.to }, context.slots.default?.())
    },
  }
  const env = { ...vue, ...appearance, ...icons, useRoute: () => route, RouterLink }
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
      for (const [name, value] of Object.entries(icons)) app.component(name, value)
      return renderToString(app)
    },
    dispose: () => scope.stop(),
  }
}

test('theme and collapse controls do not navigate or remove the collapsed navigation links', async () => {
  const sidebar = loadSidebar()
  try {
    const expanded = await sidebar.render()
    assert.match(expanded, /aria-label="切换亮色主题"/)
    assert.match(expanded, /aria-expanded="true"/)
    sidebar.toggleCollapsed()
    sidebar.toggleTheme()
    await vue.nextTick()
    const collapsed = await sidebar.render()
    assert.equal(sidebar.route.path, '/projects')
    assert.equal(sidebar.storage.getItem('api-agent.theme'), 'light')
    assert.equal(sidebar.storage.getItem('api-agent.sidebar-collapsed'), 'true')
    assert.equal(sidebar.appearance.document.documentElement.dataset.theme, 'light')
    assert.match(collapsed, /sidebar-collapsed/)
    assert.match(collapsed, /aria-expanded="false"/)
    assert.match(collapsed, /aria-label="切换暗色主题"/)
    const links = /href="([^"]+)"/g
    assert.deepEqual([...collapsed.matchAll(links)].map(match => match[1]),
      [...expanded.matchAll(links)].map(match => match[1]))
    for (const label of ['接口测试', '长连接测试', '用例测试', '项目与环境', '请求历史', '执行结果', '系统设置']) {
      assert.ok(collapsed.includes(`title="${label}"`))
      assert.ok(collapsed.includes(`aria-label="${label}"`))
    }
  } finally {
    sidebar.dispose()
  }
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
