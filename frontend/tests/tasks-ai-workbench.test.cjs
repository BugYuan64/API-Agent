const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')
const pinia = require('pinia')
const { compile } = require('@vue/compiler-dom')
const { renderToString } = require('@vue/server-renderer')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const settle = async (ticks = 10) => {
  for (let i = 0; i < ticks; i++) await Promise.resolve()
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

function loadTsModule(relPath, deps = {}) {
  const source = fs.readFileSync(path.join(__dirname, '..', relPath), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const exports = {}
  const keys = Object.keys(deps)
  new Function('exports', ...keys, outputText)(exports, ...Object.values(deps))
  return exports
}

const resultMod = loadTsModule('src/lib/results.ts')
const formatMod = loadTsModule('src/lib/format.ts')
const copyMod = loadTsModule('src/lib/copy.ts')
const prefixMod = loadTsModule('src/lib/urlPrefixes.ts')
const assistantContextMod = loadTsModule('src/lib/assistantContext.ts')
const clientMod = loadTsModule('src/api/client.ts', {
  window: { setTimeout: () => 1, clearTimeout: () => {} },
  fetch: () => { throw new Error('Network disabled in unit tests') },
})
const tasksApiMod = loadTsModule('src/api/tasks.ts', {
  require: () => clientMod,
})
const taskResultsMod = loadTsModule('src/lib/taskResults.ts', {
  require: () => resultMod,
})
const workbenchRunsMod = loadTsModule('src/stores/workbenchRuns.ts', {
  require: id => {
    if (id === 'pinia') return pinia
    if (id === 'vue') return vue
    throw new Error(`Unexpected require: ${id}`)
  },
})
const tasksWorkbenchMod = loadTsModule('src/stores/tasksWorkbench.ts', {
  require: id => {
    if (id === 'pinia') return pinia
    if (id === 'vue') return vue
    throw new Error(`Unexpected require: ${id}`)
  },
})

function loadSfc(relPath, props = {}, mocks = {}) {
  const sfc = fs.readFileSync(path.join(__dirname, '..', relPath), 'utf8')
  const script = sfc
    .split('<script setup lang="ts">')[1]
    .split('</script>')[0]
    .replace(/^\s*import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/gm, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText

  const storage = mocks.storage ?? memoryStorage()
  const chatHistoryMod = loadTsModule('src/lib/chatHistory.ts', { window: { localStorage: storage } })
  const testSettingsWindow = {
    localStorage: storage,
    addEventListener: () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  }
  const testSettingsMod = loadTsModule('src/lib/testSettings.ts', {
    require: () => vue,
    window: testSettingsWindow,
  })

  const appPinia = mocks.pinia ?? pinia.createPinia()
  pinia.setActivePinia(appPinia)

  const reactiveProps = vue.reactive({ ...props })
  const emitted = []
  const mounted = []
  const unmounted = []
  const exposed = {}
  const scope = vue.effectScope()

  const env = {
    ...vue,
    ...resultMod,
    ...formatMod,
    ...copyMod,
    ...prefixMod,
    ...assistantContextMod,
    ...chatHistoryMod,
    ...taskResultsMod,
    ...testSettingsMod,
    taskHistoryLabel: tasksApiMod.taskHistoryLabel,
    describeError: clientMod.describeError,
    ApiError: clientMod.ApiError,
    useToastStore: () => ({ push: () => {} }),
    useWorkbenchRunsStore: workbenchRunsMod.useWorkbenchRunsStore,
    useTasksWorkbenchStore: tasksWorkbenchMod.useTasksWorkbenchStore,
    defineProps: () => reactiveProps,
    defineEmits: () => (event, ...args) => emitted.push([event, ...args]),
    defineExpose: obj => Object.assign(exposed, obj),
    onMounted: fn => mounted.push(fn),
    onBeforeUnmount: fn => unmounted.push(fn),
    window: testSettingsWindow,
    ...mocks,
  }

  const bindings = []
  function collect(node) {
    if (ts.isIdentifier(node)) bindings.push(node.text)
    else node.elements.forEach(entry => { if (!ts.isOmittedExpression(entry)) collect(entry.name) })
  }
  for (const stmt of ts.createSourceFile(relPath, script, ts.ScriptTarget.Latest, true).statements) {
    if (ts.isVariableStatement(stmt)) stmt.declarationList.declarations.forEach(entry => collect(entry.name))
    if (ts.isFunctionDeclaration(stmt)) bindings.push(stmt.name.text)
  }

  const state = scope.run(() =>
    new Function(...Object.keys(env), `${js}; return { ${bindings.join(',')} }`)(...Object.values(env)),
  )

  const template = sfc.split('<template>')[1].split('<style')[0].replace(/<\/template>\s*$/, '')
  const renderJs = ts.transpileModule(
    `function build() { ${compile(template, { mode: 'function', isTS: true }).code} }`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
  ).outputText
  const render = new Function('Vue', `${renderJs}; return build()`)(vue)

  const icons = sfc.match(/import\s*\{([^}]+)\}\s*from\s*['"]lucide-vue-next['"]/)?.[1] ?? ''
  const iconNames = icons.split(',').map(v => v.trim()).filter(Boolean)

  return {
    state,
    taskSettings: testSettingsMod.taskSettings,
    props: reactiveProps,
    emitted,
    exposed,
    pinia: appPinia,
    workbenchRuns: workbenchRunsMod.useWorkbenchRunsStore(),
    tasksStore: tasksWorkbenchMod.useTasksWorkbenchStore(),
    triggerMounted: () => mounted.forEach(fn => fn()),
    unmount: () => { unmounted.forEach(fn => fn()); scope.stop() },
    render: async (componentStubs = {}) => {
      const ctx = vue.proxyRefs({
        ...env,
        ...reactiveProps,
        $slots: {},
        ...Object.fromEntries(iconNames.map(icon => [icon, {}])),
        ...state,
      })
      const app = vue.createSSRApp({ render: () => render(ctx, []) })
      for (const match of sfc.matchAll(/import\s+(\w+)(?:,\s*\{[^}]*\})?\s+from\s+['"][^'"]+\.vue['"]/g)) {
        const compName = match[1]
        if (componentStubs[compName]) {
          app.component(compName, componentStubs[compName])
        } else if (compName === 'TaskSummary') {
          app.component(compName, { props: ['text', 'status'], setup: p => () => vue.h('div', p.text || p.status) })
        } else {
          app.component(compName, {
            inheritAttrs: false,
            setup: (_p, { slots, attrs }) => () => vue.h('div', { 'data-stub': compName, ...attrs },
              Object.values(slots).flatMap(slot => slot({ item: {} }))),
          })
        }
      }
      for (const icon of iconNames) app.component(icon, { render: () => null })
      return renderToString(app)
    },
  }
}

function makeSampleCases() {
  return [
    {
      name: '查询健康状态',
      protocol: 'http',
      method: 'GET',
      path: '/health',
      source: 'document',
      kind: 'contract',
      headers: { 'X-Env': 'test' },
      assertions: [{ kind: 'status', expected: 200 }],
    },
    {
      name: '创建订单',
      protocol: 'http',
      method: 'POST',
      path: '/orders',
      source: 'generated',
      kind: 'test',
      headers: { Authorization: 'Bearer {{token}}' },
      body: { sku: 'item-1', qty: 2 },
      assertions: [{ kind: 'status', expected: 201 }],
      extract: [{ name: 'order_id', source: 'json', pointer: '/id' }],
    },
  ]
}

function makeMultiRoundTask(id = 'task-ai-1', overrides = {}) {
  const cases = makeSampleCases()
  return {
    id,
    title: 'AI 多轮订单压测',
    origin: 'http://127.0.0.1:9100',
    source: 'ai',
    badge: 'AI 测试',
    total_rounds: 2,
    current_round: 2,
    cooldown_ms: 800,
    cooldown_remaining_ms: null,
    tests_per_url: 4,
    status: 'completed',
    stage: 'finished',
    current_index: null,
    active_stream: null,
    created_at: '2026-11-27T10:00:00Z',
    error_kind: null,
    summary: '2 轮共执行 4 条用例，第 2 轮创建订单失败。',
    snapshot: {
      origin: 'http://127.0.0.1:9100',
      variables: { token: 'seed-tok', tenant: 'acme' },
      cases,
      operations: [
        { path: '/health', example_index: 0, case_indices: [0] },
        { path: '/orders', example_index: 1, case_indices: [1] },
      ],
      warnings: [],
      engine: 'native-contract-v1',
    },
    results: [
      {
        index: 0,
        case_id: 'r1:c0',
        round: 1,
        case_index: 0,
        execution_index: 0,
        name: '查询健康状态',
        protocol: 'http',
        source: 'document',
        status: 'passed',
        assertions: [{ kind: 'status', expected: 200, actual: 200, passed: true }],
      },
      {
        index: 1,
        case_id: 'r1:c1',
        round: 1,
        case_index: 1,
        execution_index: 1,
        name: '创建订单',
        protocol: 'http',
        source: 'generated',
        status: 'passed',
        assertions: [{ kind: 'status', expected: 201, actual: 201, passed: true }],
      },
      {
        index: 2,
        case_id: 'r2:c0',
        round: 2,
        case_index: 0,
        execution_index: 2,
        name: '查询健康状态',
        protocol: 'http',
        source: 'document',
        status: 'passed',
        assertions: [{ kind: 'status', expected: 200, actual: 200, passed: true }],
      },
      {
        index: 3,
        case_id: 'r2:c1',
        round: 2,
        case_index: 1,
        execution_index: 3,
        name: '创建订单',
        protocol: 'http',
        source: 'generated',
        status: 'failed',
        reason: 'status_mismatch',
        assertions: [{ kind: 'status', expected: 201, actual: 429, passed: false }],
      },
    ],
    ...overrides,
  }
}

test('taskResults multi-round indexing preserves every round without overwriting, retains variables in catalogFromHistory, and resolves case_id', () => {
  const task = makeMultiRoundTask()
  const tree = taskResultsMod.buildTaskResultTree(task.snapshot, task.results, { totalRounds: task.total_rounds })
  assert.equal(tree.length, 2)

  // /health group has both Round 1 and Round 2 results preserved
  const healthGroup = tree[0]
  assert.equal(healthGroup.cases.length, 2)
  assert.deepEqual(healthGroup.cases.map(item => [item.round, item.caseId, item.result?.status]), [
    [1, 'r1:c0', 'passed'],
    [2, 'r2:c0', 'passed'],
  ])

  // /orders group preserves Round 1 passed AND Round 2 failed (no overwriting!)
  const ordersGroup = tree[1]
  assert.equal(ordersGroup.cases.length, 2)
  assert.deepEqual(ordersGroup.cases.map(item => [item.round, item.caseId, item.result?.status]), [
    [1, 'r1:c1', 'passed'],
    [2, 'r2:c1', 'failed'],
  ])
  assert.equal(ordersGroup.outcomes.find(o => o.status === 'passed')?.count, 1)
  assert.equal(ordersGroup.outcomes.find(o => o.status === 'failed')?.count, 1)

  // Also works when results reuse index=0 across rounds instead of execution_index
  const repeatedIndexResults = [
    { index: 0, round: 1, status: 'passed', name: '查询健康状态', protocol: 'http', source: 'document', assertions: [] },
    { index: 0, round: 2, status: 'error', name: '查询健康状态', protocol: 'http', source: 'document', assertions: [] },
  ]
  const repeatedTree = taskResultsMod.buildTaskResultTree(
    { ...task.snapshot, cases: [task.snapshot.cases[0]], operations: [{ path: '/health', example_index: 0, case_indices: [0] }] },
    repeatedIndexResults,
  )
  assert.deepEqual(repeatedTree[0].cases.map(item => [item.round, item.caseId, item.result?.status]), [
    [1, 'r1:c0', 'passed'],
    [2, 'r2:c0', 'error'],
  ])

  // catalogFromHistory / historyToCatalog / buildHistoryCatalog preserve snapshot.variables
  const converted = taskResultsMod.catalogFromHistory(task.snapshot, task.title)
  assert.deepEqual(converted.variables, { token: 'seed-tok', tenant: 'acme' })
  assert.notEqual(converted.variables, task.snapshot.variables)
  assert.equal(taskResultsMod.historyToCatalog, taskResultsMod.catalogFromHistory)
  assert.equal(taskResultsMod.buildHistoryCatalog, taskResultsMod.catalogFromHistory)

  // findTaskResultByCaseId and findTaskCaseForResult locate exact multi-round target
  const matchedResult = taskResultsMod.findTaskResultByCaseId(task.results, 'r2:c1')
  assert.equal(matchedResult?.status, 'failed')
  assert.equal(matchedResult?.execution_index, 3)

  const resolved = taskResultsMod.findTaskCaseForResult(task, 'r2:c1')
  assert.ok(resolved)
  assert.equal(resolved.round, 2)
  assert.equal(resolved.caseIndex, 1)
  assert.equal(resolved.case_index, 1)
  assert.equal(resolved.testCase?.path, '/orders')
  assert.equal(resolved.result?.status, 'failed')

  // activeTaskWait works accurately in Round 2 even when Round 1 already recorded index 0
  const runningRound2 = makeMultiRoundTask('live-r2', {
    status: 'running',
    stage: 'executing',
    current_round: 2,
    current_index: 0,
    results: task.results.slice(0, 2), // Only round 1 finished
  })
  const wait = taskResultsMod.activeTaskWait(runningRound2)
  assert.ok(wait)
  assert.equal(wait.index, 0)
  assert.equal(wait.round, 2)
  assert.equal(wait.caseId, 'r2:c0')
})

test('TestCasesView removes onBeforeRouteLeave route-blocking completely', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/views/TestCasesView.vue'), 'utf8')
  assert.doesNotMatch(source, /onBeforeRouteLeave/)
  assert.doesNotMatch(source, /from\s+['"]vue-router['"]/)
})

test('TestCasesView maps AI history badges & running state, forwards tests_per_url/cooldown_ms/draftEndpoints to TaskChat & startTask, and renders cooling_down stage', async context => {
  const startPayloads = []
  const rerunPayloads = []
  let capturedChatProps = null

  const page = loadSfc('src/views/TestCasesView.vue', {}, {
    listTasks: async () => ({
      items: [
        {
          id: 'ai-running',
          title: 'AI 回归',
          source: 'ai',
          badge: 'AI 测试',
          total_rounds: 3,
          current_round: 2,
          status: 'running',
          stage: 'cooling_down',
          created_at: '2026-11-27T11:00:00Z',
          error_kind: null,
        },
        {
          id: 'manual-done',
          title: '手动测试',
          source: 'manual',
          badge: null,
          status: 'completed',
          stage: 'finished',
          created_at: '2026-11-27T10:00:00Z',
          error_kind: null,
        },
      ],
    }),
    startTask: async payload => {
      startPayloads.push(payload)
      return makeMultiRoundTask('started-1', { status: 'completed', stage: 'finished' })
    },
    rerunTask: async (id, payload) => {
      rerunPayloads.push({ id, ...payload })
      return makeMultiRoundTask('rerun-1', { status: 'completed', stage: 'finished' })
    },
    readTask: async id => makeMultiRoundTask(id, { status: 'completed', stage: 'finished' }),
  })
  context.after(page.unmount)

  await page.state.refreshHistory()
  const aiEntry = page.state.entries.value.find(item => item.id === 'ai-running')
  const manualEntry = page.state.entries.value.find(item => item.id === 'manual-done')
  assert.deepEqual(aiEntry.badges, [{ label: 'AI 测试', tone: 'info' }])
  assert.equal(aiEntry.running, true)
  assert.equal(manualEntry.badges, undefined)
  assert.equal(manualEntry.running, false)

  // Configure settings and draft catalog, verify assistantSettings, assistantContext, and startTask cooldown_ms
  page.taskSettings.testsPerUrl = 6
  page.taskSettings.cooldownMs = 1200
  page.state.origin.value = 'http://127.0.0.1:9100'
  page.state.catalog.value = {
    format: 'openapi',
    title: '订单服务',
    endpoints: makeSampleCases(),
    cases: [],
    warnings: [],
  }

  assert.equal(page.state.assistantSettings.value.tests_per_url, 6)
  assert.equal(page.state.assistantSettings.value.testsPerUrl, 6)
  assert.equal(page.state.assistantSettings.value.cooldown_ms, 1200)
  assert.equal(page.state.assistantContext.value.settings.tests_per_url, 6)
  assert.equal(page.state.assistantContext.value.settings.cooldown_ms, 1200)
  assert.equal(page.state.assistantContext.value.draft_endpoints.length, 2)

  // Verify TaskChat receives props and cooling_down stage renders in overview
  page.state.run.value = makeMultiRoundTask('cooling-task', {
    status: 'running',
    stage: 'cooling_down',
    total_rounds: 3,
    current_round: 2,
    cooldown_remaining_ms: 1500,
  })

  const html = await page.render({
    TaskChat: {
      props: ['scope', 'taskId', 'executionId', 'draftVersion', 'currentTest', 'settings', 'baseCatalog', 'activeCaseId'],
      setup: props => {
        capturedChatProps = props
        return () => vue.h('div', { class: 'chat-stub' })
      },
    },
  })
  assert.match(html, /冷却中/)
  assert.match(html, /第 2 \/ 3 轮/)
  assert.ok(capturedChatProps)
  assert.equal(capturedChatProps.scope, 'tasks')
  assert.equal(capturedChatProps.settings.tests_per_url, 6)
  assert.equal(capturedChatProps.settings.cooldown_ms, 1200)
  assert.equal(capturedChatProps.currentTest.settings.tests_per_url, 6)
  assert.equal(capturedChatProps.currentTest.settings.cooldown_ms, 1200)
  assert.equal(capturedChatProps.baseCatalog.endpoints.length, 2)

  // Submit new catalog forwards cooldown_ms
  page.state.run.value = null
  await page.state.submit()
  assert.equal(startPayloads.length, 1)
  assert.equal(startPayloads[0].cooldown_ms, 1200)
})

test('select-case locks TestCasesView in read-only replay mode, TaskDraft locks inputs & highlights focused case, and TaskResultTree expands & focuses multi-round case', async context => {
  const aiTask = makeMultiRoundTask('task-ai-replay')
  const page = loadSfc('src/views/TestCasesView.vue', {}, {
    readTask: async () => aiTask,
    listTasks: async () => ({ items: [aiTask] }),
  })
  context.after(page.unmount)

  const focusCalls = []
  page.state.resultTreeRef.value = { focusCase: id => { focusCalls.push(id); return true } }

  // Trigger @select-case from an AI CaseLinkBlock chip
  await page.state.handleSelectCase({
    type: 'case_link',
    scope: 'tasks',
    history_id: 'task-ai-replay',
    case_id: 'r2:c1',
    round: 2,
    label: '第 2 轮 · #2 创建订单',
    status: 'failed',
    tone: 'danger',
  })

  assert.equal(page.state.isReadOnlyReplay.value, true)
  assert.equal(page.state.draftLocked.value, true)
  assert.equal(page.state.focusedCaseIndex.value, 1)
  assert.equal(page.workbenchRuns.readonlyReplay.tasks?.caseId, 'r2:c1')
  assert.equal(page.tasksStore.focusedCaseId, 'r2:c1')
  assert.deepEqual(focusCalls, ['r2:c1'])

  // Mutations must be blocked while in read-only replay
  const originalPath = page.state.replayDraft.value.cases[1].path
  page.state.changeDraft({ kind: 'history', index: 1 }, { ...aiTask.snapshot.cases[1], path: '/hacked' })
  assert.equal(page.state.replayDraft.value.cases[1].path, originalPath)

  const pageHtml = await page.render()
  assert.match(pageHtml, /class="readonly-banner"/)
  assert.match(pageHtml, /只读回放/)
  assert.match(pageHtml, /第 2 轮/)
  assert.match(pageHtml, /第 2 轮 · #2 创建订单/)
  assert.match(pageHtml, /readonly-retest-btn/)

  // Verify TaskDraft readonly prop locks all inputs/buttons and auto-expands focused case
  const draft = loadSfc('src/components/TaskDraft.vue', {
    snapshot: aiTask.snapshot,
    editing: false,
    disabled: false,
    selecting: false,
    selected: [],
    readonly: true,
    focusedCaseId: 'r2:c1',
    focusedCaseIndex: 1,
  })
  context.after(draft.unmount)

  // Programmatic beginEdit must be ignored in readonly mode
  draft.state.beginEdit('op', 0, '/orders', { kind: 'history', index: 1 }, aiTask.snapshot.cases[1])
  assert.equal(draft.state.edit.value, null)

  const draftHtml = await draft.render()
  assert.match(draftHtml, /data-readonly="true"/)
  assert.match(draftHtml, /data-case-index="1"[^>]*data-focused="true"/)
  assert.match(draftHtml, /<li class="draft-case draft-case-focused" data-case-index="1" data-focused="true"><details open>/)
  assert.match(draftHtml, /当前回放/)
  assert.match(draftHtml, /<input[^>]*value="POST"[^>]*readonly[^>]*disabled/)
  assert.match(draftHtml, /<input[^>]*value="\/orders"[^>]*readonly[^>]*disabled/)
  assert.match(draftHtml, /aria-label="删除 POST \/orders" disabled/)
  assert.match(draftHtml, /<button class="btn btn-sm" type="button" disabled title="只读回放模式，点击「再次测试」后可修改">修改请求<\/button>/)

  // Verify TaskResultTree renders round badges, expands group & case on focusedCaseId, and exposes focusCase
  const scrolled = []
  const tree = loadSfc('src/components/TaskResultTree.vue', {
    task: aiTask,
    streamWait: null,
    focusedCaseId: 'r2:c1',
  })
  context.after(tree.unmount)

  assert.equal(typeof tree.exposed.focusCase, 'function')
  assert.equal(tree.state.openGroups.has('operation:1'), true)
  assert.equal(tree.state.openCases.has('r2:c1'), true)

  // Verify DOM scrollIntoView is invoked when rootRef is present (first flush initial mount watch, then call exposed focusCase)
  await vue.nextTick()
  tree.state.rootRef.value = {
    querySelectorAll: () => [
      {
        dataset: { caseId: 'r1:c0' },
        getAttribute: () => 'r1:c0',
        scrollIntoView: () => scrolled.push('r1:c0'),
      },
      {
        dataset: { caseId: 'r2:c1' },
        getAttribute: () => 'r2:c1',
        scrollIntoView: () => scrolled.push('r2:c1'),
      },
    ],
  }
  assert.equal(tree.exposed.focusCase('r2:c1'), true)
  await vue.nextTick()
  assert.deepEqual(scrolled, ['r2:c1'])

  const treeHtml = await tree.render()
  assert.match(treeHtml, /第 1 轮/)
  assert.match(treeHtml, /第 2 轮/)
  assert.match(treeHtml, /case-focused/)
  assert.match(treeHtml, /data-case-id="r2:c1"[^>]*data-focused="true"/)
})

test('testAgain ("再次测试") unlocks read-only replay, preserves editable catalog with variables, clears run, and increments draftVersion for fresh blank chat', async context => {
  const aiTask = makeMultiRoundTask('task-finished-ai')
  const page = loadSfc('src/views/TestCasesView.vue', {}, {
    readTask: async () => aiTask,
    listTasks: async () => ({ items: [aiTask] }),
  })
  context.after(page.unmount)

  await page.state.handleSelectCase({
    type: 'case_link',
    scope: 'tasks',
    history_id: 'task-finished-ai',
    case_id: 'r2:c1',
    round: 2,
    label: '第 2 轮 · #2 创建订单',
    status: 'failed',
    tone: 'danger',
  })
  assert.equal(page.state.isReadOnlyReplay.value, true)
  const beforeVersion = page.state.draftVersion.value

  // Click "再次测试"
  page.state.testAgain()

  assert.equal(page.state.isReadOnlyReplay.value, false)
  assert.equal(page.state.readonlyReplay.value, null)
  assert.equal(page.workbenchRuns.readonlyReplay.tasks, null)
  assert.equal(page.workbenchRuns.activeHistoryId.tasks, null)
  assert.equal(page.state.run.value, null)
  assert.equal(page.state.draftVersion.value, beforeVersion + 1)
  assert.equal(page.state.origin.value, 'http://127.0.0.1:9100')
  assert.ok(page.state.catalog.value)
  assert.equal(page.state.catalog.value.endpoints.length, 2)
  assert.deepEqual(page.state.catalog.value.variables, { token: 'seed-tok', tenant: 'acme' })
  assert.equal(page.state.draftLocked.value, false)

  // Editing is unlocked on the prepared catalog
  page.state.changeDraft(
    { kind: 'example', operation: 0 },
    { ...page.state.catalog.value.endpoints[0], path: '/health?fresh=1' },
  )
  assert.equal(page.state.catalog.value.endpoints[0].path, '/health?fresh=1')

  // newTest clears everything and increments draftVersion again
  page.state.newTest()
  assert.equal(page.state.origin.value, '')
  assert.equal(page.state.catalog.value, null)
  assert.equal(page.state.run.value, null)
  assert.equal(page.state.readonlyReplay.value, null)
  assert.equal(page.state.draftVersion.value, beforeVersion + 2)
})

test('TestCasesView cross-page unmount never cancels running task and remount auto-reconnects followTask with live progress', async () => {
  const sharedPinia = pinia.createPinia()
  const firstFollow = deferred()
  const secondFollow = deferred()
  let firstSignal = null
  let secondOnFrame = null
  let cancelCalls = 0
  const runningTask = makeMultiRoundTask('bg-task-1', {
    status: 'running',
    stage: 'executing',
    current_round: 1,
    current_index: 1,
    results: makeMultiRoundTask().results.slice(0, 1),
    summary: null,
  })

  // First mount: AI binds a running task via @run-bound
  const first = loadSfc('src/views/TestCasesView.vue', {}, {
    pinia: sharedPinia,
    listTasks: async () => ({ items: [runningTask] }),
    readTask: async () => runningTask,
    followTask: (_id, signal) => {
      firstSignal = signal
      return firstFollow.promise
    },
    cancelTask: async () => {
      cancelCalls++
      return runningTask
    },
  })

  await first.state.handleRunBound({
    scope: 'tasks',
    history_id: 'bg-task-1',
    previous_history_id: null,
    badge: 'AI 测试',
    title: 'AI 多轮订单压测',
    forked: true,
    run_id: 'asst-1',
  })
  await settle()

  assert.equal(first.state.run.value?.id, 'bg-task-1')
  assert.equal(first.workbenchRuns.activeRunId.tasks, 'bg-task-1')
  assert.equal(first.tasksStore.activeTaskId, 'bg-task-1')
  assert.ok(firstSignal && !firstSignal.aborted)

  // Navigate to another route: unmount aborts local followTask observer ONLY, NEVER cancels backend task or clears store
  first.unmount()
  firstFollow.reject(new Error('observer aborted'))
  await settle()

  assert.equal(firstSignal.aborted, true)
  assert.equal(cancelCalls, 0)
  assert.equal(first.workbenchRuns.activeRunId.tasks, 'bg-task-1')
  assert.equal(first.tasksStore.run?.id, 'bg-task-1')
  assert.equal(first.tasksStore.origin, 'http://127.0.0.1:9100')

  // Navigate back to Smart Group Testing: remount restores store and auto-reconnects followTask
  const reconnectedIds = []
  const second = loadSfc('src/views/TestCasesView.vue', {}, {
    pinia: sharedPinia,
    listTasks: async () => ({ items: [runningTask] }),
    readTask: async () => runningTask,
    followTask: (id, _signal, onFrame) => {
      reconnectedIds.push(id)
      secondOnFrame = onFrame
      return secondFollow.promise
    },
    cancelTask: async () => {
      cancelCalls++
      return runningTask
    },
  })

  // State hydrated synchronously before mount completes
  assert.equal(second.state.origin.value, 'http://127.0.0.1:9100')
  assert.equal(second.state.run.value?.id, 'bg-task-1')

  second.triggerMounted()
  await settle()

  assert.deepEqual(reconnectedIds, ['bg-task-1'])
  assert.equal(typeof secondOnFrame, 'function')

  // Stream cooling_down frame followed by completion
  secondOnFrame({
    type: 'progress',
    status: 'running',
    stage: 'cooling_down',
    current_round: 1,
    total_rounds: 2,
    current_index: null,
    cooldown_remaining_ms: 500,
    active_stream: null,
    error_kind: null,
    results: makeMultiRoundTask().results.slice(0, 2),
    events: [],
    events_seen: 0,
    summary: null,
  }, true)
  assert.equal(second.state.run.value.stage, 'cooling_down')
  assert.equal(second.state.run.value.results.length, 2)

  secondOnFrame({
    type: 'progress',
    status: 'completed',
    stage: 'finished',
    current_round: 2,
    total_rounds: 2,
    current_index: null,
    cooldown_remaining_ms: null,
    active_stream: null,
    error_kind: null,
    results: makeMultiRoundTask().results.slice(2),
    events: [],
    events_seen: 0,
    summary: '最终总结已生成',
  }, false)
  secondFollow.resolve()
  await settle()

  assert.equal(second.state.run.value.status, 'completed')
  assert.equal(second.state.run.value.results.length, 4)
  assert.equal(second.state.run.value.summary, '最终总结已生成')
  assert.equal(second.workbenchRuns.activeRunId.tasks, null)
  assert.equal(cancelCalls, 0)
  second.unmount()
})

test('TestCasesView preserves test document across selectHistory, testAgain, removeOperation, route switch, and submit, and keeps primary 开始测试 button clickable with label 开始测试', async context => {
  const sharedPinia = pinia.createPinia()
  const docText = JSON.stringify({
    openapi: '3.0.3',
    info: { title: '客户服务' },
    paths: {
      '/health': { get: { responses: { '200': { description: 'ok' } } } },
      '/orders': { post: { responses: { '201': { description: 'created' } } } },
    },
  })
  const docTask = (id, overrides = {}) => makeMultiRoundTask(id, {
    title: '客户服务',
    source: 'manual',
    badge: null,
    total_rounds: 1,
    current_round: 1,
    results: makeMultiRoundTask().results.slice(0, 2),
    snapshot: {
      ...makeMultiRoundTask().snapshot,
      document: { filename: 'customer-api.json' },
    },
    ...overrides,
  })

  const startPayloads = []
  const rerunPayloads = []
  const docReads = []
  let createdSeq = 0

  const mocks = {
    pinia: sharedPinia,
    listTasks: async () => ({ items: [docTask('hist-doc-1')] }),
    readTask: async id => docTask(id),
    readTaskDocument: async id => {
      docReads.push(id)
      return docText
    },
    startTask: async payload => {
      startPayloads.push(payload)
      const id = `started-doc-${++createdSeq}`
      return docTask(id, {
        snapshot: {
          ...docTask(id).snapshot,
          ...(payload.document ? { document: { filename: payload.document.filename } } : { document: undefined }),
        },
      })
    },
    rerunTask: async (id, payload) => {
      rerunPayloads.push({ id, ...payload })
      return docTask(`rerun-doc-${++createdSeq}`)
    },
  }

  const page = loadSfc('src/views/TestCasesView.vue', {}, mocks)
  context.after(page.unmount)

  // 1. Selecting a history item with a document preserves filename, download link, and keeps primary button as clickable "开始测试"
  await page.state.selectHistory('hist-doc-1')
  await settle()

  assert.equal(page.state.displayedFilename.value, 'customer-api.json')
  assert.equal(page.state.documentDownloadId.value, 'hist-doc-1')
  assert.equal(page.state.hasDocument.value, true)
  assert.equal(page.state.origin.value, 'http://127.0.0.1:9100')
  assert.equal(page.state.draftLocked.value, false)

  const historyHtml = await page.render()
  assert.match(historyHtml, /href="\/api\/tasks\/hist-doc-1\/document"[^>]*download[^>]*>customer-api\.json<\/a>/)
  assert.match(historyHtml, /<div class="actions"><button class="btn btn-primary"><div data-stub="AppIcon" name="play"><\/div>开始测试<\/button>/)
  assert.doesNotMatch(historyHtml, /<div class="actions"><button[^>]*>(?:(?!<\/button>)[\s\S])*再次测试<\/button>/)
  assert.doesNotMatch(historyHtml, /<div class="actions"><button class="btn btn-primary" disabled/)

  // 2. Clicking submit on history triggers rerunTask (not startTask) and keeps button label "开始测试"
  await page.state.submit()
  await settle()
  assert.equal(rerunPayloads.length, 1)
  assert.equal(rerunPayloads[0].id, 'hist-doc-1')
  assert.equal(page.state.displayedFilename.value, 'customer-api.json')
  const postRerunHtml = await page.render()
  assert.match(postRerunHtml, /<div class="actions"><button class="btn btn-primary">[\s\S]*开始测试<\/button>/)

  // 3. Clicking testAgain ("再次测试" in banner/header) unlocks catalog while retaining document filename, documentInput & download link
  page.state.testAgain()
  assert.equal(page.state.replaySource.value, null)
  assert.ok(page.state.catalog.value)
  assert.equal(page.state.displayedFilename.value, 'customer-api.json')
  assert.equal(page.state.filename.value, 'customer-api.json')
  assert.equal(page.state.hasDocument.value, true)
  assert.equal(page.state.draftLocked.value, false)

  // 4. Removing an operation, editing a case, and switching away/back via tasksWorkbench store preserves document
  page.state.removeOperation(0)
  assert.equal(page.state.catalog.value.endpoints.length, 1)
  page.unmount()

  const remounted = loadSfc('src/views/TestCasesView.vue', {}, mocks)
  context.after(remounted.unmount)
  assert.equal(remounted.state.displayedFilename.value, 'customer-api.json')
  assert.equal(remounted.state.hasDocument.value, true)
  assert.equal(remounted.state.catalog.value.endpoints.length, 1)

  const unlockedHtml = await remounted.render()
  assert.match(unlockedHtml, /<div class="actions"><button class="btn btn-primary">[\s\S]*开始测试<\/button>/)
  assert.doesNotMatch(unlockedHtml, /<div class="actions"><button class="btn btn-primary" disabled/)

  await remounted.state.submit()
  await settle()
  assert.equal(startPayloads.length, 1)
  assert.deepEqual(startPayloads[0].document, { filename: 'customer-api.json', text: docText })
  assert.equal(remounted.state.displayedFilename.value, 'customer-api.json')

  // 5. Even if testAgain runs BEFORE prefetch finishes (e.g. cold documentInput), sourceDocument still loads from documentSourceId
  const coldStartPayloads = []
  const coldPage = loadSfc('src/views/TestCasesView.vue', {}, {
    listTasks: async () => ({ items: [docTask('cold-1')] }),
    readTask: async id => docTask(id),
    readTaskDocument: async () => docText,
    startTask: async payload => {
      coldStartPayloads.push(payload)
      return docTask('cold-started')
    },
  })
  context.after(coldPage.unmount)
  await coldPage.state.selectHistory('cold-1')
  coldPage.state.documentInput.value = null
  coldPage.state.testAgain()
  assert.equal(coldPage.state.displayedFilename.value, 'customer-api.json')
  assert.equal(coldPage.state.hasDocument.value, true)
  await coldPage.state.submit()
  assert.equal(coldStartPayloads.length, 1)
  assert.deepEqual(coldStartPayloads[0].document, { filename: 'customer-api.json', text: docText })
  assert.ok(docReads.length >= 1)
})

test('TestCasesView handles AI-triggered tasks without a document cleanly and all 4 workbenches keep primary submit button labels stable', async context => {
  const aiNoDoc = makeMultiRoundTask('ai-no-doc-1', {
    status: 'completed',
    stage: 'finished',
  })
  const rerunPayloads = []
  const startPayloads = []

  const page = loadSfc('src/views/TestCasesView.vue', {}, {
    listTasks: async () => ({ items: [aiNoDoc] }),
    readTask: async () => aiNoDoc,
    rerunTask: async (id, payload) => {
      rerunPayloads.push({ id, ...payload })
      return makeMultiRoundTask('ai-no-doc-rerun')
    },
    startTask: async payload => {
      startPayloads.push(payload)
      return makeMultiRoundTask('ai-no-doc-start')
    },
  })
  context.after(page.unmount)

  await page.state.handleRunBound({
    scope: 'tasks',
    history_id: 'ai-no-doc-1',
    previous_history_id: null,
    badge: 'AI 测试',
    title: 'AI 多轮订单压测',
    forked: true,
    run_id: 'asst-no-doc',
  })
  await settle()

  assert.equal(page.state.displayedFilename.value, '')
  assert.equal(page.state.hasDocument.value, false)
  assert.equal(page.state.draftLocked.value, false)

  const html = await page.render()
  assert.match(html, /未上传接口文档/)
  assert.match(html, /<div class="actions"><button class="btn btn-primary">[\s\S]*开始测试<\/button>/)
  assert.doesNotMatch(html, /<div class="actions"><button class="btn btn-primary" disabled/)

  await page.state.submit()
  assert.equal(rerunPayloads.length, 1)
  assert.equal(rerunPayloads[0].id, 'ai-no-doc-1')

  page.state.testAgain()
  await page.state.submit()
  assert.equal(startPayloads.length, 1)
  assert.equal('document' in startPayloads[0], false)

  // Guard all 4 primary action buttons against switching into "再次测试"
  const tasksSfc = fs.readFileSync(path.join(__dirname, '../src/views/TestCasesView.vue'), 'utf8')
  const modelSfc = fs.readFileSync(path.join(__dirname, '../src/views/ModelTestingView.vue'), 'utf8')
  const consoleSfc = fs.readFileSync(path.join(__dirname, '../src/views/RequestConsole.vue'), 'utf8')
  const streamSfc = fs.readFileSync(path.join(__dirname, '../src/views/LongConnection.vue'), 'utf8')
  assert.match(tasksSfc, /<div class="actions"><button class="btn btn-primary"[^>]*><AppIcon name="play" \/>开始测试<\/button>/)
  assert.doesNotMatch(tasksSfc, /replaySource\s*\?\s*'再次测试'\s*:\s*'开始测试'/)
  assert.match(modelSfc, /`开始探测（\$\{selectedIds\.length\}）`/)
  assert.match(consoleSfc, /<span>\{\{ sending \? '发送中' : '发送' \}\}<\/span>/)
  assert.match(streamSfc, /\{\{ connecting \? '连接中' : '开始连接' \}\}/)
})
