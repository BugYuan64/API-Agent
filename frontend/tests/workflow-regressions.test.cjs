const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')
const { compile } = require('@vue/compiler-dom')
const { renderToString } = require('@vue/server-renderer')

// Reuse production result classification inside executed SFC setup scripts.
const resultExports = {}
new Function('exports', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/results.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(resultExports)
const clientExports = {}
new Function('exports', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/api/client.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(clientExports)

// Execute the production SFC setup scripts with real Vue reactivity. Only the
// API, lifecycle registration and browser clock are substituted; no HTTP/model.
globalThis.fetch = () => { throw new Error('Network is prohibited in frontend tests') }

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function load(name, names, mocks = {}) {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/views', `${name}.vue`), 'utf8')
  const source = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/g, '')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const timers = new Map()
  const unmount = []
  let timerId = 0
  const scope = vue.effectScope()
  const env = {
    ...vue,
    ...resultExports,
    ApiError: clientExports.ApiError,
    onMounted: () => {},
    onBeforeUnmount: fn => unmount.push(fn),
    describeError: error => String(error),
    useToastStore: () => ({ push: () => {} }),
    // 视图从 `lib/engines` 取引擎列表与选择状态；这些测试只关心轮询与诊断行为，
    // 这里给出等价形状的桩，使 SFC 脚本仍可被原样求值。
    useEngineSelection: () => ({
      engines: vue.ref([]),
      loading: vue.ref(false),
      selected: vue.ref('native'),
      activeEngine: vue.ref(null),
      refresh: async () => {},
      select: () => {},
    }),
    // 面板现在通过 defineEmits 向外壳上报历史、通过 defineExpose 暴露 openHistory；
    // 测试只关心原有业务行为，这里给出等价桩。
    defineEmits: () => () => {},
    defineExpose: () => {},
    defineProps: () => ({}),
    withDefaults: (props) => props,
    engineLabel: id => id,
    engineMeta: id => ({ name: id, kind: '内置', description: '', transports: '' }),
    useHeaderRows: () => ({ rows: vue.ref([{ name: '', value: '' }]), headers: vue.ref({}),
      count: vue.ref(0), add: () => {}, remove: () => {}, reset: () => {} }),
    formatRelative: () => '刚刚',
    formatDateTime: value => value ?? '—',
    formatDuration: value => `${value}ms`,
    // AutoPane 用别名导入 HTTP 状态码色调（内部另有同名生命周期函数），
    // 测试原先只桩了 format* 系列，别名缺失会让断言证据渲染直接抛错。
    httpStatusTone: value => (value >= 500 ? 'danger' : value >= 400 ? 'warning'
      : value >= 300 ? 'info' : value >= 200 ? 'success' : 'neutral'),
    window: {
      localStorage: { getItem: () => null },
      setTimeout: fn => { timers.set(++timerId, { fn, repeat: false }); return timerId },
      clearTimeout: id => timers.delete(id),
      setInterval: fn => { timers.set(++timerId, { fn, repeat: true }); return timerId },
      clearInterval: id => timers.delete(id),
    },
    ...mocks,
  }
  const pollSource = fs.readFileSync(path.join(__dirname, '../src/composables/usePolling.ts'), 'utf8')
  const pollJs = ts.transpileModule(pollSource, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const pollExports = {}
  new Function('require', 'exports', 'window', pollJs)(
    () => ({ ...vue, onBeforeUnmount: fn => unmount.push(fn) }), pollExports, env.window,
  )
  env.usePolling = pollExports.usePolling
  const exposed = names.map(key => `${key}: typeof ${key} === 'undefined' ? undefined : ${key}`).join(',')
  // 编译原模板并复用实际 setup 状态，检查用户看见的结果，而非检查源码字符串。
  const bindings = []
  function bindingNames(node) {
    if (ts.isIdentifier(node)) bindings.push(node.text)
    else node.elements.forEach(entry => { if (!ts.isOmittedExpression(entry)) bindingNames(entry.name) })
  }
  for (const node of ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true).statements) {
    if (ts.isVariableStatement(node)) node.declarationList.declarations.forEach(entry => bindingNames(entry.name))
    if (ts.isFunctionDeclaration(node)) bindings.push(node.name.text)
  }
  const api = scope.run(() => new Function(...Object.keys(env),
    `${js};return {${exposed}, templateState: {${bindings.join(',')}}}`)(...Object.values(env)))
  const template = sfc.split('<template>')[1].split('</script>')[0].split('<style')[0].replace(/<\/template>\s*$/, '')
  const renderJs = ts.transpileModule(
    `function build() { ${compile(template, { mode: 'function', isTS: true }).code} }`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
  ).outputText
  const render = new Function('Vue', `${renderJs}; return build()`)(vue)
  return {
    ...api,
    render: async () => {
      const icons = sfc.match(/import\s*\{([^}]+)\}\s*from\s*['"]lucide-vue-next['"]/)?.[1] ?? ''
      const iconNames = icons.split(',').map(value => value.trim()).filter(Boolean)
      const context = vue.proxyRefs({ ...env, $slots: {}, ...Object.fromEntries(iconNames.map(icon => [icon, {}])), ...api.templateState })
      const app = vue.createSSRApp({ render: () => render(context, []) })
      // 这里只替代布局与图标等展示依赖，真实面板模板、结果分类和响应式状态均执行。
      for (const component of sfc.matchAll(/import\s+(\w+)\s+from\s+['"][^'"]+\.vue['"]/g)) {
        app.component(component[1], { setup: (_, { slots }) => () => vue.h('div', Object.values(slots).flatMap(slot => slot())) })
      }
      for (const icon of iconNames) app.component(icon, { render: () => null })
      return renderToString(app)
    },
    tick: () => {
      const [id, timer] = [...timers][0] ?? []
      assert.equal(typeof timer?.fn, 'function', 'expected a scheduled poll')
      if (!timer.repeat) timers.delete(id)
      return timer.fn()
    },
    timerCount: () => timers.size,
    dispose: () => { unmount.forEach(fn => fn()); scope.stop() },
  }
}

test('console clears previous success while waiting and keeps browser timeout outside target results', async () => {
  const pending = deferred()
  const page = load('RequestConsole', ['send', 'result', 'localFailure'], {
    sendConsoleRequest: () => pending.promise,
  })
  page.result.value = { ok: true, response: { status: 200 } }
  const sending = page.send()
  assert.equal(page.result.value, null)
  pending.reject(new clientExports.ApiError(0, 'client_timeout', 'timeout'))
  await sending
  assert.equal(page.result.value, null)
  assert.equal(page.localFailure.value.code, 'BROWSER_TIMEOUT')
  assert.equal(page.localFailure.value.resultStatus, null)
  page.dispose()
})

const taskRecord = (id, status = 'running') => ({ id, status, stage: 'executing',
  created_at: '2026-11-16T00:00:00Z', snapshot: { cases: [], warnings: [] }, results: [], summary: null })

test('switching task history rejects late progress for the previous task', async () => {
  const late = deferred()
  const page = load('TestCasesView', ['run', 'openHistory'], {
    readTask: id => id === 'old' ? late.promise : Promise.resolve(taskRecord('new', 'completed')),
    listTasks: async () => ({ items: [] }),
  })
  const pending = page.openHistory('old')
  await page.openHistory('new')
  late.resolve(taskRecord('old'))
  await pending
  assert.equal(page.run.value.id, 'new')
  assert.equal(page.timerCount(), 0)
  page.dispose()
})

test('unmount cancels task polling and ignores late progress', async () => {
  const late = deferred()
  let signal
  const page = load('TestCasesView', ['run', 'openHistory'], {
    readTask: (id, input) => { signal = input; return late.promise },
  })
  const pending = page.openHistory('old')
  page.dispose()
  assert.equal(signal.aborted, true)
  late.resolve(taskRecord('old'))
  await pending
  assert.equal(page.run.value, null)
  assert.equal(page.timerCount(), 0)
})

test('task cancellation cannot be overwritten by an in-flight running snapshot', async () => {
  const late = deferred()
  let calls = 0
  const page = load('TestCasesView', ['run', 'openHistory', 'cancel'], {
    readTask: () => ++calls === 1 ? late.promise : Promise.resolve(taskRecord('same', 'cancelled')),
    cancelTask: async () => taskRecord('same', 'cancelled'),
    listTasks: async () => ({ items: [] }),
  })
  page.run.value = taskRecord('same')
  const pending = page.openHistory('same')
  await page.cancel()
  late.resolve(taskRecord('same'))
  await pending
  assert.equal(page.run.value.status, 'cancelled')
  assert.equal(page.timerCount(), 0)
  page.dispose()
})
