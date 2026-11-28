const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')

function loadTs(relPath, deps = {}) {
  const src = fs.readFileSync(path.join(__dirname, '..', relPath), 'utf8')
  const { outputText } = ts.transpileModule(src, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const exports = {}
  new Function('exports', ...Object.keys(deps), outputText)(exports, ...Object.values(deps))
  return exports
}

test('TestWorkspace hides history rail when container width falls below 3-pane minimum (e.g. 900x800)', () => {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/components/TestWorkspace.vue'), 'utf8')
  const script = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[^\n]+from[^\n]+\r?\n/g, '')
  const { outputText } = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const layout = loadTs('src/lib/workspaceLayout.ts')
  const split = loadTs('src/lib/split.ts', { window: { localStorage: { getItem: () => null, setItem: () => {} } } })
  const mounts = []
  const cleanups = []
  let width = 1200
  let notifyResize
  const scope = vue.effectScope()
  const env = {
    ...vue,
    ...layout,
    ...split,
    bounds: layout.WORKSPACE_LAYOUT_BOUNDS,
    defineProps: () => ({
      historyKey: 'h',
      editorKey: 'e',
      assistantKey: 'a',
      editorWidth: 360,
      hideHistoryOnNarrow: true,
    }),
    onMounted: fn => mounts.push(fn),
    onBeforeUnmount: fn => cleanups.push(fn),
    ResizeObserver: class { constructor(cb) { notifyResize = cb } observe() {} disconnect() {} },
    window: {
      matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    },
  }
  const api = scope.run(() =>
    new Function(...Object.keys(env), `${outputText}; return { root, historyHidden, widths }`)(...Object.values(env)),
  )
  api.root.value = { getBoundingClientRect: () => ({ width }) }
  mounts.forEach(fn => fn())
  assert.equal(api.historyHidden.value, false)

  // Simulate 900x800 viewport where sidebar leaves ~690px for TestWorkspace (< 840px 3-pane min)
  width = 690
  notifyResize()
  assert.equal(api.historyHidden.value, true)
  assert.equal(api.widths.value.history, 0)
  assert.ok(api.widths.value.editor >= 320 && api.widths.value.results >= 320)

  // Expanding back restores history rail
  width = 1200
  notifyResize()
  assert.equal(api.historyHidden.value, false)
  assert.ok(api.widths.value.history >= 200)

  cleanups.forEach(fn => fn())
  scope.stop()
})

test('AppTopbar watches selectedEngine and refreshes engine version on switch', async () => {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/components/AppTopbar.vue'), 'utf8')
  const script = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[^\n]+from[^\n]+\r?\n/g, '')
  const { outputText } = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const selected = vue.ref('native')
  const probed = []
  const mounts = []
  const scope = vue.effectScope()
  const env = {
    ...vue,
    getHealth: async () => ({ status: 'ok' }),
    engineStatus: async id => {
      probed.push(id)
      return id === 'native'
        ? { available: true, version: '1.1.0', engine: 'native' }
        : { available: true, version: '2.4.0', engine: 'postman-cli' }
    },
    engineMeta: id => ({ name: id === 'postman' ? 'Postman CLI 引擎' : '本地原生引擎' }),
    useEngineSelection: () => ({ selected, refresh: async () => {} }),
    onMounted: fn => mounts.push(fn),
  }
  const api = scope.run(() =>
    new Function(...Object.keys(env), `${outputText}; return { engineLabel, refresh }`)(...Object.values(env)),
  )
  await api.refresh()
  assert.equal(api.engineLabel.value, '本地原生引擎 v1.1.0')

  selected.value = 'postman'
  await vue.nextTick()
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.deepEqual(probed, ['native', 'postman'])
  assert.equal(api.engineLabel.value, 'Postman CLI 引擎 v2.4.0')
  scope.stop()
})

test('all 4 workbenches enable hide-history-on-narrow, wrap toolbars cleanly, and ModelTestingView avoids 200px head-actions overflow', () => {
  for (const name of ['RequestConsole', 'LongConnection', 'TestCasesView', 'ModelTestingView']) {
    const src = fs.readFileSync(path.join(__dirname, '../src/views', `${name}.vue`), 'utf8')
    assert.match(src, /<TestWorkspace[\s\S]*?hide-history-on-narrow/, `${name} must set hide-history-on-narrow`)
  }
  const req = fs.readFileSync(path.join(__dirname, '../src/views/RequestConsole.vue'), 'utf8')
  assert.match(req, /\.response-stats\s*\{[\s\S]*?flex-wrap:\s*wrap/)
  const lc = fs.readFileSync(path.join(__dirname, '../src/views/LongConnection.vue'), 'utf8')
  assert.match(lc, /\.timeline-toolbar\s*\{[\s\S]*?flex-wrap:\s*wrap/)
  const mt = fs.readFileSync(path.join(__dirname, '../src/views/ModelTestingView.vue'), 'utf8')
  assert.doesNotMatch(mt, /template #head-actions[\s\S]*?retest-head-btn/)
  const engines = loadTs('src/lib/engines.ts', {
    require: id => (id === 'vue' ? vue : { engineList: async () => ({ items: [], default: 'native' }) }),
  })
  assert.equal(engines.engineMeta('postman-cli').name, 'Postman CLI 引擎')
})

test('AppTopbar syncs version immediately from activeEngine cache before async status resolves', async () => {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/components/AppTopbar.vue'), 'utf8')
  const script = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[^\n]+from[^\n]+\r?\n/g, '')
  const { outputText } = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const selected = vue.ref('native')
  const engines = vue.ref([
    { id: 'native', available: true, version: '1.1.0', engine: 'native' },
    { id: 'postman', available: true, version: '1.82.0', engine: 'postman-cli' },
  ])
  const activeEngine = vue.computed(() => engines.value.find(item => item.id === selected.value) ?? null)
  let resolveSlowProbe
  const scope = vue.effectScope()
  const env = {
    ...vue,
    getHealth: async () => ({ status: 'ok' }),
    engineStatus: id => {
      if (id === 'postman') {
        return new Promise(resolve => {
          resolveSlowProbe = resolve
        })
      }
      return Promise.resolve({ available: true, version: '1.1.0', engine: 'native' })
    },
    engineMeta: id => ({ name: id === 'postman' ? 'Postman CLI 引擎' : '本地原生引擎' }),
    useEngineSelection: () => ({ selected, activeEngine, refresh: async () => {} }),
    onMounted: () => {},
  }
  const api = scope.run(() =>
    new Function(...Object.keys(env), `${outputText}; return { engineLabel, refresh }`)(...Object.values(env)),
  )
  await api.refresh()
  assert.equal(api.engineLabel.value, '本地原生引擎 v1.1.0')

  // Switch to postman: even while slow CLI probe is still pending, cached activeEngine version renders immediately.
  selected.value = 'postman'
  await vue.nextTick()
  assert.equal(api.engineLabel.value, 'Postman CLI 引擎 v1.82.0')

  // Once async probe finishes with an updated version, topbar reflects it too.
  resolveSlowProbe({ available: true, version: '1.83.0', engine: 'postman-cli' })
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(api.engineLabel.value, 'Postman CLI 引擎 v1.83.0')
  scope.stop()
})

test('ModelTestingView pins col-actions sticky right and RequestConsole/LongConnection wrap narrow toolbars', () => {
  const mt = fs.readFileSync(path.join(__dirname, '../src/views/ModelTestingView.vue'), 'utf8')
  assert.match(mt, /:editor-width="320"/)
  assert.match(mt, /\.col-actions\s*\{[\s\S]*?position:\s*sticky;[\s\S]*?right:\s*0;/)

  const req = fs.readFileSync(path.join(__dirname, '../src/views/RequestConsole.vue'), 'utf8')
  assert.match(req, /\.request-head\s*\{[\s\S]*?flex-wrap:\s*wrap/)
  assert.match(req, /\.url-bar\s*\{[\s\S]*?flex-wrap:\s*wrap/)
  assert.match(req, /\.tabs\s*\{[\s\S]*?flex-wrap:\s*wrap/)

  const lc = fs.readFileSync(path.join(__dirname, '../src/views/LongConnection.vue'), 'utf8')
  assert.match(lc, /\.composer-foot\s*\{[\s\S]*?flex-wrap:\s*wrap/)
  assert.match(lc, /\.actions\s*\{[\s\S]*?flex-wrap:\s*wrap/)
  assert.match(lc, /\.stage-foot\s*\{[\s\S]*?flex-wrap:\s*wrap/)
  assert.match(lc, /readonlyReplay\.notes/)
})
