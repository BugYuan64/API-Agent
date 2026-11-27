const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')
const pinia = require('pinia')
const { compile } = require('@vue/compiler-dom')
const { renderToString } = require('@vue/server-renderer')

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

function loadTsModule(relPath, extraDeps = {}) {
  const source = fs.readFileSync(path.join(__dirname, '..', relPath), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const exports = {}
  const keys = Object.keys(extraDeps)
  new Function('exports', ...keys, outputText)(exports, ...Object.values(extraDeps))
  return exports
}

function loadTestSettings(storage = memoryStorage()) {
  const listeners = new Map()
  const mockWindow = {
    localStorage: storage,
    addEventListener: (event, fn) => listeners.set(event, fn),
    removeEventListener: event => listeners.delete(event),
  }
  const exports = loadTsModule('src/lib/testSettings.ts', {
    require: id => {
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
    window: mockWindow,
  })
  return { ...exports, storage, triggerStorage: event => listeners.get('storage')?.(event) }
}

function renderComponent(relPath, props = {}, mocks = {}, slots = {}) {
  const sfc = fs.readFileSync(path.join(__dirname, '..', relPath), 'utf8')
  const script = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/^\s*import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/gm, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText

  const reactiveProps = vue.reactive({ ...props })
  const scope = vue.effectScope()
  const env = {
    ...vue,
    exports: {},
    defineProps: () => reactiveProps,
    withDefaults: (p, defaults) => Object.assign(reactiveProps, { ...defaults, ...p }),
    defineEmits: () => () => {},
    defineExpose: () => {},
    useToastStore: () => ({ push: () => {} }),
    ...mocks,
  }

  const bindings = []
  function collectBindings(node) {
    if (ts.isIdentifier(node)) bindings.push(node.text)
    else node.elements.forEach(entry => { if (!ts.isOmittedExpression(entry)) collectBindings(entry.name) })
  }
  for (const stmt of ts.createSourceFile(relPath, script, ts.ScriptTarget.Latest, true).statements) {
    if (ts.isVariableStatement(stmt)) stmt.declarationList.declarations.forEach(entry => collectBindings(entry.name))
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
    props: reactiveProps,
    render: async () => {
      const ctx = vue.proxyRefs({
        ...env,
        ...reactiveProps,
        $slots: slots,
        ...Object.fromEntries(iconNames.map(icon => [icon, {}])),
        ...state,
      })
      const app = vue.createSSRApp({ render: () => render(ctx, []) })
      for (const icon of iconNames) app.component(icon, { render: () => null })
      return renderToString(app)
    },
    dispose: () => scope.stop(),
  }
}

test('HistoryList renders badges after label and before delete, supports legacy badge, running dot, and preserves slots', async context => {
  const items = [
    {
      id: 'ai-run',
      label: 'POST /api/orders 超长标题会被省略但徽标不能被压扁',
      tone: 'info',
      status: '运行中',
      meta: '刚刚',
      running: true,
      badge: 'AI 测试',
      badges: [
        { label: 'AI 测试', tone: 'info', title: '由小Y自动编排' },
        { label: '3 轮', tone: 'warning', title: '共 3 轮循环测试' },
      ],
    },
    {
      id: 'legacy-badge',
      label: 'GET /api/health',
      tone: 'success',
      status: '200',
      meta: '1 分钟前',
      badge: 'AI 回归',
    },
    {
      id: 'plain',
      label: 'GET /api/plain',
      tone: 'neutral',
      status: '200',
      meta: '2 分钟前',
    },
  ]

  const comp = renderComponent('src/components/HistoryList.vue', {
    title: '请求历史',
    items,
    emptyText: '暂无记录',
    activeId: 'ai-run',
    onDelete: () => {},
    onNewTest: null,
  }, {}, {
    'head-actions': () => [vue.h('span', { class: 'slot-head-marker' }, '头部插槽')],
    prefix: ({ item }) => [vue.h('span', { class: 'slot-prefix-marker' }, item.id)],
    actions: ({ item }) => [vue.h('span', { class: 'slot-actions-marker' }, `act:${item.id}`)],
  })
  context.after(comp.dispose)

  const html = await comp.render()

  // Slots preserved
  assert.match(html, /class="slot-head-marker">头部插槽/)
  assert.match(html, /class="slot-prefix-marker">ai-run/)
  assert.match(html, /class="slot-actions-marker">act:ai-run/)

  // Running dot appears only once (for ai-run)
  assert.equal((html.match(/class="history-running-dot"/g) ?? []).length, 1)

  // Badges appear immediately after .history-label and before .history-delete, without duplicating 'AI 测试'
  const firstItemEnd = html.indexOf('GET /api/health')
  const firstItemHtml = html.slice(0, firstItemEnd)
  const labelPos = firstItemHtml.indexOf('class="history-label truncate"')
  const badgesPos = firstItemHtml.indexOf('class="history-badges"')
  const deletePos = firstItemHtml.indexOf('class="history-delete"')
  assert.ok(labelPos > 0 && badgesPos > labelPos && deletePos > badgesPos)
  assert.equal((firstItemHtml.match(/AI 测试/g) ?? []).length, 1)
  assert.match(firstItemHtml, /title="由小Y自动编排"/)
  assert.match(firstItemHtml, /history-badge-warning[^>]*title="共 3 轮循环测试"[^>]*>\s*3 轮\s*</)

  // Legacy single badge fallback renders on second item
  assert.match(html, /history-badge-info[^>]*title="AI 回归"[^>]*>\s*AI 回归\s*</)

  // CSS guarantees flex: none on badges so they never collapse
  const sfcSource = fs.readFileSync(path.join(__dirname, '../src/components/HistoryList.vue'), 'utf8')
  assert.match(sfcSource, /\.history-badges\s*\{[\s\S]*?flex:\s*none;/)
  assert.match(sfcSource, /\.history-badge\s*\{[\s\S]*?flex:\s*none;/)
})

test('workspaceLayout and TestWorkspace support configurable resultsMax override while keeping default 720 intact', () => {
  const layout = loadTsModule('src/lib/workspaceLayout.ts')
  const split = loadTsModule('src/lib/split.ts', { window: { localStorage: memoryStorage() } })

  // Default remains 720 and immutable
  assert.equal(layout.WORKSPACE_LAYOUT_BOUNDS.results.max, 720)
  assert.equal(layout.workspaceMaxWidth, 360 + 720 + 720)
  assert.equal(layout.getWorkspaceMaxWidth(), 1800)
  assert.equal(layout.getWorkspaceMaxWidth(1400), 360 + 720 + 1400)
  assert.deepEqual(layout.resolveWorkspaceBounds({ resultsMax: 1400 }).results, { min: 320, max: 1400 })
  assert.deepEqual(layout.resolveWorkspaceBounds(100).results, { min: 320, max: 320 })

  // allocateWorkspaceWidth expands results up to 1400 when overridden (number or options object)
  const initial = { history: 260, editor: 600, results: 640 }
  assert.deepEqual(layout.allocateWorkspaceWidth(2200, initial, false, 1400), { history: 260, editor: 720, results: 1220 })
  assert.deepEqual(layout.allocateWorkspaceWidth(2600, initial, false, { resultsMax: 1400 }), { history: 360, editor: 720, results: 1400 })
  // Default without override still caps at 720
  assert.deepEqual(layout.allocateWorkspaceWidth(2600, initial), { history: 360, editor: 720, results: 720 })

  // adjacentWidthRange respects resultsMax override when dragging editor divider
  const wideWidths = { history: 260, editor: 500, results: 1100 }
  assert.deepEqual(layout.adjacentWidthRange(wideWidths, 'editor', false, 1400), { min: 320, max: 720 })
  assert.deepEqual(layout.adjacentWidthRange({ history: 260, editor: 500, results: 1200 }, 'editor', false, 1400), { min: 320, max: 720 })
  assert.deepEqual(layout.adjacentWidthRange({ history: 260, editor: 500, results: 1300 }, 'editor', false, 1400), { min: 400, max: 720 })

  // Mount real TestWorkspace.vue with resultsMax: 1400
  const sfc = fs.readFileSync(path.join(__dirname, '../src/components/TestWorkspace.vue'), 'utf8')
  const script = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[^\n]+from[^\n]+\r?\n/g, '')
  const { outputText } = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2022 } })
  const mounts = []
  const cleanup = []
  const scope = vue.effectScope()
  const props = vue.reactive({
    historyKey: 'h',
    editorKey: 'e',
    assistantKey: 'a',
    editorWidth: 360,
    resultsMax: 1400,
  })
  const env = {
    ...vue,
    ...split,
    ...layout,
    bounds: layout.WORKSPACE_LAYOUT_BOUNDS,
    defineProps: () => props,
    onMounted: fn => mounts.push(fn),
    onBeforeUnmount: fn => cleanup.push(fn),
    ResizeObserver: class { observe() {} disconnect() {} },
  }
  const api = scope.run(() =>
    new Function(...Object.keys(env), `${outputText}; return { root, widths, shellStyle, editorRange }`)(...Object.values(env)),
  )
  api.root.value = { getBoundingClientRect: () => ({ width: 2200 }) }
  mounts.forEach(fn => fn())
  try {
    assert.equal(api.widths.value.history, 200)
    assert.equal(api.widths.value.editor, 600)
    assert.equal(api.widths.value.results, 1400)
    assert.equal(api.shellStyle.value['--workspace-results-max'], '1400px')
  } finally {
    cleanup.forEach(fn => fn())
    scope.stop()
  }
})

test('testSettings and TestSettingsFields support cooldownMs across all 4 groups and persist/reset modelTestingSettings', async context => {
  const storage = memoryStorage()
  const mod = loadTestSettings(storage)

  assert.equal(mod.consoleSettings.cooldownMs, 500)
  assert.equal(mod.webSocketSettings.cooldownMs, 500)
  assert.equal(mod.taskSettings.cooldownMs, 500)
  assert.deepEqual({ ...mod.modelTestingSettings }, { timeoutSeconds: 20, concurrency: 4, cooldownMs: 500 })

  // Editing modelTestingSettings persists to dedicated localStorage key
  mod.modelTestingSettings.timeoutSeconds = 45
  mod.modelTestingSettings.concurrency = 8
  mod.modelTestingSettings.cooldownMs = 1200
  assert.deepEqual(
    JSON.parse(storage.getItem('api-agent.settings.model_testing')),
    { timeoutSeconds: 45, concurrency: 8, cooldownMs: 1200 },
  )

  // Reload restores persisted modelTestingSettings
  const reloaded = loadTestSettings(storage)
  assert.deepEqual({ ...reloaded.modelTestingSettings }, { timeoutSeconds: 45, concurrency: 8, cooldownMs: 1200 })

  // resetTestSettings restores defaults and clears storage keys
  reloaded.resetTestSettings()
  assert.deepEqual({ ...reloaded.modelTestingSettings }, { timeoutSeconds: 20, concurrency: 4, cooldownMs: 500 })
  assert.equal(storage.getItem('api-agent.settings.model_testing'), null)
  assert.equal(storage.getItem('api-agent.test-settings'), null)

  // TestSettingsFields renders cooldownMs for all 4 groups and clamps out-of-range edits
  const toasts = []
  for (const group of ['console', 'websocket', 'task', 'model_testing']) {
    const freshMod = loadTestSettings(memoryStorage())
    const comp = renderComponent('src/components/TestSettingsFields.vue', {
      group,
      disabled: false,
      generationLocked: false,
    }, {
      ...freshMod,
      useToastStore: () => ({ push: (...args) => toasts.push(args) }),
    })
    context.after(comp.dispose)

    const html = await comp.render()
    assert.match(html, /每轮冷却 \(ms\)/)
    assert.match(html, /step="100"/)

    const cooldownField = comp.state.fields.value.find(f => f.key === 'cooldownMs')
    assert.ok(cooldownField, `cooldownMs field missing for ${group}`)
    const lowInput = { valueAsNumber: -100, value: '-100' }
    comp.state.updateNumber({ currentTarget: lowInput }, cooldownField)
    assert.equal(lowInput.value, '0')
    assert.equal(freshMod.testSettings[group].cooldownMs, 0)

    const highInput = { valueAsNumber: 90000, value: '90000' }
    comp.state.updateNumber({ currentTarget: highInput }, cooldownField)
    assert.equal(highInput.value, '60000')
    assert.equal(freshMod.testSettings[group].cooldownMs, 60000)
    assert.match(toasts.at(-1)[1], /超出范围/)
  }
})

test('assistantContext supports 4-page contexts, task settings/draftEndpoints, and strict model testing redaction', () => {
  const ctxMod = loadTsModule('src/lib/assistantContext.ts')

  // Console context with settings + URL credential redaction
  const consoleCtx = ctxMod.buildConsoleTestContext({
    method: 'POST',
    url: 'https://user:pass@api.example.com/v1/orders?api_key=sk-secret-123&page=2',
    headers: { Authorization: 'Bearer top-secret', 'X-Trace': 'ok' },
    bodyMode: 'json',
    body: '{"item":"book"}',
    result: null,
    liveEvents: null,
    localFailure: null,
    responseCleared: false,
    settings: { timeoutMs: 30000, cooldownMs: 800, followRedirects: true },
  })
  assert.equal(consoleCtx.page, 'console')
  assert.equal(consoleCtx.request.headers.Authorization, '[redacted]')
  assert.equal(consoleCtx.request.headers['X-Trace'], 'ok')
  assert.ok(!consoleCtx.request.url.includes('pass'))
  assert.ok(!consoleCtx.request.url.includes('sk-secret-123'))
  assert.match(consoleCtx.request.url, /page=2/)
  assert.deepEqual(consoleCtx.settings, { timeout_ms: 30000, cooldown_ms: 800, follow_redirects: true })

  // WebSocket context with settings
  const wsCtx = ctxMod.buildWebSocketTestContext({
    url: 'wss://ws.example.com/chat?token=ws-secret&room=1',
    headers: { Cookie: 'sid=123' },
    subprotocols: ['v1'],
    session: null,
    events: [],
    settings: { sendIntervalMs: 300, cooldownMs: 1000 },
  })
  assert.equal(wsCtx.page, 'websocket')
  assert.equal(wsCtx.request.headers.Cookie, '[redacted]')
  assert.ok(!wsCtx.request.url.includes('ws-secret'))
  assert.deepEqual(wsCtx.settings, { send_interval_ms: 300, cooldown_ms: 1000 })

  // Task context with settings (tests_per_url + cooldown_ms) and draftEndpoints
  const taskCtx = ctxMod.buildTaskTestContext({
    origin: 'http://127.0.0.1:8088',
    run: null,
    resultsCleared: false,
    draftEndpointCount: 2,
    draftCaseCount: 6,
    settings: { testsPerUrl: 5, allowWrites: false, observationMs: 15000, wallTimeSeconds: 600, cooldownMs: 750 },
    draftEndpoints: [
      { method: 'GET', path: '/api/items', protocol: 'http', name: '查询列表' },
      { method: 'POST', path: '/api/items', protocol: 'http' },
    ],
  })
  assert.equal(taskCtx.page, 'tasks')
  assert.equal(taskCtx.has_result, false)
  assert.deepEqual(taskCtx.settings, {
    tests_per_url: 5,
    allow_writes: false,
    observation_ms: 15000,
    wall_time_seconds: 600,
    cooldown_ms: 750,
  })
  assert.deepEqual(taskCtx.draft_endpoints, [
    { method: 'GET', path: '/api/items', protocol: 'http', name: '查询列表' },
    { method: 'POST', path: '/api/items', protocol: 'http' },
  ])

  // Model testing context: aggregates summary and NEVER leaks raw apiKey
  const sampleModels = [
    {
      id: 'gpt-4o',
      owned_by: 'openai',
      created: null,
      model_type: 'chat',
      type_source: 'inferred',
      availability: { status: 'available', source: 'probed', latency_ms: 210, detail: 'ok' },
      tool_calling: { status: 'supported', source: 'probed', latency_ms: 250, detail: 'tool_calls' },
      streaming: { status: 'supported', source: 'probed', latency_ms: 300, ttft_ms: 90, detail: 'sse' },
      structured_output: { status: 'supported', source: 'probed', latency_ms: 220, detail: 'schema' },
      input_modalities: [{ modality: 'text', status: 'supported', source: 'probed', detail: null }],
      output_modalities: [{ modality: 'text', status: 'supported', source: 'probed', detail: null }],
      raw_metadata: { secret_hint: 'never-include' },
    },
    {
      id: 'broken-model',
      owned_by: 'openai',
      created: null,
      model_type: 'chat',
      type_source: 'inferred',
      availability: { status: 'unavailable', source: 'probed', latency_ms: 100, detail: 'HTTP 502' },
      tool_calling: { status: 'unsupported', source: 'probed', latency_ms: null, detail: null },
      streaming: { status: 'unsupported', source: 'probed', latency_ms: null, ttft_ms: null, detail: null },
      structured_output: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
      input_modalities: [],
      output_modalities: [],
      raw_metadata: {},
    },
  ]

  const modelCtx = ctxMod.buildModelTestingContext({
    baseUrl: 'https://admin:pw123@proxy.example.com/v1?api_key=sk-raw-secret',
    protocol: 'openai',
    useSavedKey: false,
    apiKey: 'sk-super-raw-custom-key-999',
    timeoutSeconds: 30,
    checks: { availability: true, tool_calling: true, streaming: true, structured_output: false },
    cooldownMs: 600,
    models: sampleModels,
    activeModelId: 'gpt-4o',
  })

  assert.equal(modelCtx.page, 'model_testing')
  assert.equal(modelCtx.has_result, true)
  assert.equal(modelCtx.config.protocol, 'openai')
  assert.equal(modelCtx.config.use_saved_key, false)
  assert.equal(modelCtx.config.has_custom_api_key, true)
  assert.equal(modelCtx.config.timeout_seconds, 30)
  assert.deepEqual(modelCtx.config.checks, ['availability', 'tool_calling', 'streaming'])
  assert.equal(modelCtx.config.cooldown_ms, 600)
  assert.deepEqual(modelCtx.summary, {
    total: 2,
    available: 1,
    unavailable: 1,
    tool_supported: 1,
    streaming: 1,
    structured: 1,
  })
  assert.equal(modelCtx.active_model.id, 'gpt-4o')
  assert.equal(modelCtx.active_model.ttft_ms, 90)
  assert.equal(modelCtx.models_preview.length, 2)

  // Strict zero-secret guarantee across serialized context
  const serialized = JSON.stringify(modelCtx)
  assert.ok(!serialized.includes('sk-super-raw-custom-key-999'))
  assert.ok(!serialized.includes('sk-raw-secret'))
  assert.ok(!serialized.includes('pw123'))
  assert.ok(!serialized.includes('never-include'))
})

test('workbenchRuns Pinia store isolates per-scope runtime handles and readonly replay across route switches', () => {
  const storeMod = loadTsModule('src/stores/workbenchRuns.ts', {
    require: id => {
      if (id === 'pinia') return pinia
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
  })

  assert.deepEqual(storeMod.PAGE_SCOPES, ['console', 'websocket', 'tasks', 'model_testing'])
  const appPinia = pinia.createPinia()
  pinia.setActivePinia(appPinia)
  const store = storeMod.useWorkbenchRunsStore()

  // Initial state is null across all 4 scopes
  for (const scope of storeMod.PAGE_SCOPES) {
    assert.equal(store.activeRunId[scope], null)
    assert.equal(store.activeHistoryId[scope], null)
    assert.equal(store.activeAssistantRunId[scope], null)
    assert.equal(store.readonlyReplay[scope], null)
  }

  store.setActiveRun('console', 'run-http-1')
  store.setActiveHistory('console', 'hist-http-1')
  store.setActiveAssistantRun('console', 'asst-1')
  store.enterReadonlyReplay('console', { historyId: 'hist-http-1', caseId: 'r1:c0', round: 1, label: '空参边界' })

  store.setActiveRun('model_testing', 'probe-run-9')
  store.setActiveHistory('tasks', 'task-7')

  assert.equal(store.activeRunId.console, 'run-http-1')
  assert.equal(store.activeHistoryId.console, 'hist-http-1')
  assert.equal(store.activeAssistantRunId.console, 'asst-1')
  assert.deepEqual(store.readonlyReplay.console, { historyId: 'hist-http-1', caseId: 'r1:c0', round: 1, label: '空参边界' })
  assert.equal(store.activeRunId.model_testing, 'probe-run-9')
  assert.equal(store.activeHistoryId.tasks, 'task-7')

  store.exitReadonlyReplay('console')
  assert.equal(store.readonlyReplay.console, null)
  assert.equal(store.activeRunId.console, 'run-http-1')

  // resetScope only clears the target scope
  store.resetScope('console')
  assert.equal(store.activeRunId.console, null)
  assert.equal(store.activeHistoryId.console, null)
  assert.equal(store.activeAssistantRunId.console, null)
  assert.equal(store.activeRunId.model_testing, 'probe-run-9')
  assert.equal(store.activeHistoryId.tasks, 'task-7')
})

test('router defines canonical meta.scope for all 4 test pages while preserving route names and redirects', () => {
  let capturedConfig = null
  const routerMod = loadTsModule('src/router.ts', {
    require: id => {
      if (id === 'vue-router') {
        return {
          createWebHistory: () => 'history',
          createRouter: config => {
            capturedConfig = config
            return { afterEach: () => {} }
          },
        }
      }
      throw new Error(`Unexpected require: ${id}`)
    },
  })

  assert.ok(routerMod.router)
  assert.ok(capturedConfig)
  const byPath = new Map(capturedConfig.routes.map(route => [route.path, route]))

  assert.equal(byPath.get('/request').name, 'request')
  assert.equal(byPath.get('/request').meta.scope, 'console')
  assert.equal(byPath.get('/request').meta.title, '接口测试')

  assert.equal(byPath.get('/long-connection').name, 'long-connection')
  assert.equal(byPath.get('/long-connection').meta.scope, 'websocket')
  assert.equal(byPath.get('/long-connection').meta.title, 'WebSocket 调试')

  assert.equal(byPath.get('/testcases').name, 'testcases')
  assert.equal(byPath.get('/testcases').meta.scope, 'tasks')
  assert.equal(byPath.get('/testcases').meta.title, '智能群测')

  assert.equal(byPath.get('/model-testing').name, 'model-testing')
  assert.equal(byPath.get('/model-testing').meta.scope, 'model_testing')
  assert.equal(byPath.get('/model-testing').meta.title, '大模型测试')

  // Redirects remain intact
  assert.equal(byPath.get('/').redirect, '/request')
  assert.equal(byPath.get('/plan').redirect, '/testcases')
  assert.equal(byPath.get('/agent').redirect, '/testcases')
})
