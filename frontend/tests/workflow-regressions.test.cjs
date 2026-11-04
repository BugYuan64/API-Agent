const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')

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
  const api = scope.run(() => new Function(...Object.keys(env), `${js};return {${exposed}}`)(...Object.values(env)))
  return {
    ...api,
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

test('task list refresh stays single-flight and discards the previous generation', async () => {
  const first = deferred(), second = deferred()
  let calls = 0
  const page = load('RunsView', ['load', 'runs'], {
    listRuns: () => ++calls === 1 ? first.promise : second.promise,
  })
  page.load()
  page.load()
  assert.equal(calls, 1)
  first.resolve({ items: [{ id: 'stale', status: 'completed' }] })
  await vue.nextTick()
  await Promise.resolve()
  if (calls === 1 && page.timerCount()) { page.tick(); await Promise.resolve() }
  assert.equal(calls, 2)
  second.resolve({ items: [{ id: 'current', status: 'completed' }] })
  await vue.nextTick()
  assert.equal(page.runs.value[0].id, 'current')
  page.dispose()
})

test('task details discard late responses after leaving the page', async () => {
  const pending = deferred()
  const page = load('RunDetail', ['load', 'run'], {
    useRoute: () => ({ params: { runId: 'A' } }), useRouter: () => ({}),
    getRun: () => pending.promise,
    listRunEvents: async () => ({ items: [] }), listRunBudget: async () => [],
    listRunAttempts: async () => [],
  })
  page.load()
  page.dispose()
  pending.resolve({ id: 'A', status: 'running' })
  for (let i = 0; i < 10; i++) await Promise.resolve()
  assert.equal(page.run.value, null)
  assert.equal(page.timerCount(), 0)
})

test('task detail navigation keeps one identity for the entire evidence batch', async () => {
  const first = deferred(), route = vue.reactive({ params: { runId: 'A' } }), ids = []
  const page = load('RunDetail', ['load', 'run', 'events'], {
    useRoute: () => route, useRouter: () => ({}),
    getRun: id => { ids.push(['run', id]); return id === 'A' ? first.promise : Promise.resolve({ id, status: 'completed' }) },
    listRunEvents: async id => { ids.push(['events', id]); return { items: [{ run_id: id }] } },
    listRunBudget: async id => { ids.push(['budget', id]); return [] },
    listRunAttempts: async id => { ids.push(['attempts', id]); return [] },
  })
  page.load()
  route.params.runId = 'B'
  await vue.nextTick()
  first.resolve({ id: 'A', status: 'running' })
  for (let i = 0; i < 10; i++) await Promise.resolve()
  assert.equal(page.run.value, null)
  page.tick()
  for (let i = 0; i < 10; i++) await Promise.resolve()
  assert.equal(page.run.value.id, 'B')
  assert.equal(page.events.value[0].run_id, 'B')
  assert.deepEqual(ids.map(([, id]) => id), ['A', 'A', 'A', 'A', 'B', 'B', 'B', 'B'])
  page.dispose()
})

function agent(mocks) {
  return load('AutoPane', ['form', 'discover', 'canSubmit', 'buildPayload', 'selectedPathList',
    'run', 'startPolling', 'cancel', 'isRunning', 'loadReport', 'report', 'submit'], mocks)
}
function planner(mocks) {
  return load('ManualPane', ['planText', 'result', 'diagnosis', 'diagnosing', 'analyzeWithAI',
    'selectExecution', 'canAnalyze', 'planSummary'], mocks)
}
const execution = id => ({ id, plan: { origin: 'http://127.0.0.1:8010', allowed_methods: ['GET'],
  path_templates: ['/health'], requests: [], max_requests: 1 },
  plan_sha256: 'saved-plan-hash', project_id: null, label: 'fixture', engine: 'native', status: 'passed',
  error_kind: null, duration_ms: 1, reserved_requests: 0, observed_attempts: 0, unknown_requests: 0,
  executions: [], created_at: '2026-11-02T00:00:00Z' })
const diagnosis = summary => ({ evidence: [], analysis: { summary, hypotheses: [], coverage_gaps: [] } })
const run = (status = 'running', id = 'run-A') => ({ id, status, schema_version: '1',
  created_at: '2026-11-02T00:00:00Z', report_status: 'partial', stop_reason: null,
  snapshot: {}, snapshot_sha256: 'fixture-hash', actual_llm_status: 'not_called',
  degradation_reasons: [], rounds: [], events: [], model_calls: [], commentary: null,
  budget: { target_http: { limit: 1, reserved: 0, consumed: 0 },
    model_call: { limit: 1, reserved: 0, consumed: 0 } } })

test('R07: a first plain-text contract can run with manually confirmed path templates', () => {
  const page = agent()
  page.form.contractText = 'GET /health should return status=ok'
  page.form.origin = 'http://127.0.0.1:8010'
  page.form.manualPaths = '/health\n/users/{id}\n/health'
  assert.equal(page.canSubmit.value, true)
  assert.deepEqual(page.buildPayload().scope.path_templates, ['/health', '/users/{id}'])
  page.dispose()
})

test('R07: JSON scalar/array contracts never crash discovery or send a non-object contract', () => {
  const page = agent()
  for (const text of ['null', '[]', '42', 'true', '"description"']) {
    page.form.contractText = text
    assert.doesNotThrow(() => page.discover())
    assert.equal(page.buildPayload().contract, text)
  }
  page.dispose()
})

test('R07: OpenAPI checkbox paths and manual paths combine without losing explicit choices', () => {
  const page = agent()
  page.form.contractText = '{"paths":{"/health":{"get":{}},"/users":{"post":{}}}}'
  page.discover()
  page.form.selectedPaths.delete('/users')
  page.form.manualPaths = '/extra\n/health'
  assert.deepEqual(page.buildPayload().scope.path_templates, ['/health', '/extra'])
  page.dispose()
})

test('R06: diagnosis submits saved execution identity and contract, never the edited plan or result', async () => {
  let sent
  const page = planner({ diagnoseExecution: async (...args) => { sent = args; return diagnosis('saved') },
    diagnosePlan: async (...args) => { sent = args; return diagnosis('unsafe') } })
  page.result.value = execution('execution-A')
  page.planText.value = '{"requests":[{"id":"changed"}]}'
  await page.analyzeWithAI()
  assert.deepEqual(sent, ['execution-A', { contract: '（未提供契约；仅就断言差异作观察）' }])
  page.dispose()
})

test('R06: legacy history without a stored plan cannot invoke AI diagnosis', async () => {
  let calls = 0
  const invoke = async () => { calls++; return diagnosis('legacy') }
  const page = planner({ diagnoseExecution: invoke, diagnosePlan: invoke })
  page.result.value = { ...execution('legacy'), plan: null, plan_sha256: null }
  await page.analyzeWithAI()
  assert.equal(calls, 0)
  page.dispose()
})

for (const change of ['execution', 'editor']) {
  test(`R06: ${change} change discards an in-flight diagnosis and clears previous analysis`, async () => {
    const waiting = deferred()
    const page = planner({ diagnoseExecution: () => waiting.promise, diagnosePlan: () => waiting.promise })
    page.result.value = execution('execution-A')
    const pending = page.analyzeWithAI()
    if (change === 'execution') page.result.value = execution('execution-B')
    else page.planText.value = '{"requests":[]}'
    await vue.nextTick()
    waiting.resolve(diagnosis('execution-A'))
    await pending
    assert.equal(page.diagnosis.value, null)
    page.diagnosis.value = diagnosis('old')
    page.planText.value = '{"requests":[],"origin":"changed"}'
    await vue.nextTick()
    assert.equal(page.diagnosis.value, null)
    page.dispose()
  })
}

test('R06: selecting saved history restores its plan; legacy history cannot retain a misleading plan', () => {
  const page = planner()
  assert.equal(typeof page.selectExecution, 'function')
  const saved = execution('saved')
  page.selectExecution(saved)
  assert.deepEqual(JSON.parse(page.planText.value), saved.plan)
  page.selectExecution({ ...execution('legacy'), plan: null, plan_sha256: null })
  assert.equal(page.planText.value, '')
  assert.equal(page.canAnalyze.value, false)
  page.dispose()
})

test('R06: JSON null and malformed request entries do not crash plan summary', () => {
  const page = planner()
  for (const text of ['null', '[]', '{"requests":[null,4]}']) {
    page.planText.value = text
    assert.doesNotThrow(() => page.planSummary.value)
  }
  page.dispose()
})

test('R08: polling never schedules a second GET while the first is pending', async () => {
  const waiting = deferred()
  const page = agent({ getAgentRun: () => waiting.promise })
  page.run.value = run()
  page.startPolling('run-A')
  const pending = page.tick()
  assert.equal(page.timerCount(), 0)
  waiting.resolve(run())
  await pending
  assert.equal(page.timerCount(), 1)
  page.dispose()
})

test('R08: restarting polling keeps a pending GET single-flight across generations', async () => {
  const waiting = deferred()
  let calls = 0
  const page = agent({ getAgentRun: () => { calls++; return waiting.promise } })
  page.run.value = run()
  page.startPolling('run-A')
  const old = page.tick()
  page.startPolling('run-A')
  const restarted = page.tick()
  assert.equal(calls, 1)
  waiting.resolve(run())
  await Promise.all([old, restarted])
  assert.equal(page.run.value.status, 'running')
  assert.equal(page.timerCount(), 1)
  page.dispose()
})

test('R08: a late GET cannot replace cancellation with running', async () => {
  const waiting = deferred()
  const page = agent({ getAgentRun: () => waiting.promise, cancelAgentRun: async () => run('cancelled') })
  page.run.value = run()
  page.startPolling('run-A')
  const pending = page.tick()
  await page.cancel()
  waiting.resolve(run())
  await pending
  assert.equal(page.run.value.status, 'cancelled')
  assert.equal(page.timerCount(), 0)
  page.dispose()
})

test('R08: unmount invalidates in-flight GET and report writes', async () => {
  const getting = deferred(), reporting = deferred()
  const page = agent({ getAgentRun: () => getting.promise, getAgentReport: () => reporting.promise })
  page.run.value = run()
  page.startPolling('run-A')
  const poll = page.tick()
  const report = page.loadReport()
  page.dispose()
  getting.resolve(run('completed'))
  reporting.resolve('late report')
  await Promise.all([poll, report])
  assert.equal(page.run.value.status, 'running')
  assert.equal(page.report.value, null)
  assert.equal(page.timerCount(), 0)
})

test('R08: a report from an old run cannot attach to the new run', async () => {
  const waiting = deferred()
  const page = agent({ getAgentReport: () => waiting.promise })
  page.run.value = run()
  const pending = page.loadReport()
  page.run.value = run('running', 'run-B')
  waiting.resolve('report for run-A')
  await pending
  assert.equal(page.report.value, null)
  page.dispose()
})

test('R08: a stale poll cannot regress a terminal state of the same run', async () => {
  const waiting = deferred()
  const page = agent({ getAgentRun: () => waiting.promise })
  page.run.value = run()
  page.startPolling('run-A')
  const pending = page.tick()
  page.run.value = run('completed')
  waiting.resolve(run())
  await pending
  assert.equal(page.run.value.status, 'completed')
  assert.equal(page.timerCount(), 0)
  page.dispose()
})
