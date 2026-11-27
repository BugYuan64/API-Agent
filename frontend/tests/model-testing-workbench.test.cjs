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
  let resolve
  let reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    dump: () => Object.fromEntries(values.entries()),
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

const clientExports = loadTsModule('src/api/client.ts')
const resultExports = loadTsModule('src/lib/results.ts')
const taskResultExports = loadTsModule('src/lib/taskResults.ts', { require: () => resultExports })
const prefixExports = loadTsModule('src/lib/urlPrefixes.ts')
const formatExports = loadTsModule('src/lib/format.ts')
const assistantContextExports = loadTsModule('src/lib/assistantContext.ts')
const taskExports = loadTsModule('src/api/tasks.ts', { require: () => clientExports })

function loadChatHistory(storage) {
  return loadTsModule('src/lib/chatHistory.ts', { window: { localStorage: storage } })
}

function loadStores() {
  const req = id => {
    if (id === 'pinia') return pinia
    if (id === 'vue') return vue
    throw new Error(`Unexpected require: ${id}`)
  }
  const workbenchRunsMod = loadTsModule('src/stores/workbenchRuns.ts', { require: req })
  const modelWorkbenchMod = loadTsModule('src/stores/modelTestingWorkbench.ts', { require: req })
  return {
    useWorkbenchRunsStore: workbenchRunsMod.useWorkbenchRunsStore,
    useModelTestingWorkbenchStore: modelWorkbenchMod.useModelTestingWorkbenchStore,
  }
}

function loadView(name, names, mocks = {}) {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/views', `${name}.vue`), 'utf8')
  const source = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/^\s*import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/gm, '')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText

  const mounts = []
  const unmounts = []
  const scope = vue.effectScope()
  const { storage = memoryStorage(), ...dependencies } = mocks

  const mockWindow = {
    localStorage: storage,
    matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    confirm: () => true,
    addEventListener: () => {},
    removeEventListener: () => {},
    setTimeout: fn => { fn(); return 1 },
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  }

  const testSettingsMod = loadTsModule('src/lib/testSettings.ts', {
    require: id => {
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
    window: mockWindow,
  })

  const env = {
    ...vue,
    ...assistantContextExports,
    ...loadChatHistory(storage),
    ...resultExports,
    ...taskResultExports,
    ...prefixExports,
    ...formatExports,
    ...testSettingsMod,
    taskHistoryLabel: taskExports.taskHistoryLabel,
    ApiError: clientExports.ApiError,
    describeError: error => (error instanceof Error ? error.message : String(error)),
    onMounted: fn => mounts.push(fn),
    onBeforeUnmount: fn => unmounts.push(fn),
    useToastStore: () => ({ push: () => {} }),
    window: mockWindow,
    ...dependencies,
  }

  const bindings = []
  function collectBindings(node) {
    if (ts.isIdentifier(node)) bindings.push(node.text)
    else node.elements.forEach(entry => { if (!ts.isOmittedExpression(entry)) collectBindings(entry.name) })
  }
  for (const stmt of ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true).statements) {
    if (ts.isVariableStatement(stmt)) stmt.declarationList.declarations.forEach(entry => collectBindings(entry.name))
    if (ts.isFunctionDeclaration(stmt)) bindings.push(stmt.name.text)
  }

  const exposed = names.map(key => `${key}: typeof ${key} === 'undefined' ? undefined : ${key}`).join(',')
  const api = scope.run(() =>
    new Function(
      ...Object.keys(env),
      `${js}; return { ${[exposed, `templateState: { ${bindings.join(',')} }`].filter(Boolean).join(',')} }`,
    )(...Object.values(env)),
  )

  const template = sfc.split('<template>')[1].split('<style')[0].replace(/<\/template>\s*$/, '')
  const renderJs = ts.transpileModule(
    `function build() { ${compile(template, { mode: 'function', isTS: true }).code} }`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
  ).outputText
  const render = new Function('Vue', `${renderJs}; return build()`)(vue)

  const icons = sfc.match(/import\s*\{([^}]+)\}\s*from\s+['"]lucide-vue-next['"]/)?.[1] ?? ''
  const iconNames = icons.split(',').map(v => v.trim()).filter(Boolean)

  return {
    ...api,
    mount: async () => {
      for (const fn of mounts) {
        await fn()
      }
      await Promise.resolve()
    },
    render: async () => {
      const ctx = vue.proxyRefs({
        ...env,
        $slots: {},
        ...Object.fromEntries(iconNames.map(icon => [icon, {}])),
        ...api.templateState,
      })
      const app = vue.createSSRApp({ render: () => render(ctx, []) })
      for (const match of sfc.matchAll(/import\s+(\w+)(?:,\s*\{[^}]*\})?\s+from\s+['"][^'"]+\.vue['"]/g)) {
        const compName = match[1]
        app.component(compName, {
          inheritAttrs: false,
          setup: (_, { attrs, slots }) => () => vue.h(
            'div',
            { 'data-stub': compName, ...attrs },
            Object.values(slots).flatMap(slot => slot({ item: {} })),
          ),
        })
      }
      for (const icon of iconNames) app.component(icon, { render: () => null })
      return renderToString(app)
    },
    dispose: () => {
      unmounts.forEach(fn => fn())
      scope.stop()
    },
  }
}

function sampleProfile(id, overrides = {}) {
  return {
    id,
    owned_by: 'openai',
    created: null,
    model_type: 'chat',
    type_source: 'inferred',
    availability: { status: 'available', source: 'probed', latency_ms: 185, detail: 'HTTP 200' },
    tool_calling: { status: 'supported', source: 'probed', latency_ms: 210, detail: 'tool_calls' },
    streaming: { status: 'supported', source: 'probed', latency_ms: 240, ttft_ms: 68, detail: 'SSE OK' },
    structured_output: { status: 'supported', source: 'probed', latency_ms: 190, detail: 'json_schema' },
    input_modalities: [
      { modality: 'text', status: 'supported', source: 'probed', detail: null },
      { modality: 'image', status: 'supported', source: 'probed', detail: '1x1 PNG' },
    ],
    output_modalities: [
      { modality: 'text', status: 'supported', source: 'probed', detail: null },
    ],
    raw_metadata: { id },
    ...overrides,
  }
}

function sampleRunDetail(id, overrides = {}) {
  const profile = sampleProfile('gpt-4o')
  return {
    id,
    title: 'AI 模型探测 · gpt-4o @ api.example.com（2 轮）',
    source: 'ai',
    badge: 'AI 测试',
    status: 'completed',
    error_message: null,
    base_url: 'https://api.example.com/v1',
    protocol: 'openai',
    resolved_protocol: 'openai',
    used_saved_key: false,
    has_custom_api_key: true,
    timeout_seconds: 20,
    concurrency: 4,
    checks: ['availability', 'tool_calling', 'streaming'],
    target_models: ['gpt-4o'],
    cooldown_ms: 500,
    total_rounds: 2,
    completed_rounds: 2,
    sub_case_count: 2,
    summary: {
      tested_count: 1,
      total_cases: 2,
      completed_cases: 2,
      available_count: 1,
      unavailable_count: 0,
      error_count: 0,
      tool_supported_count: 1,
      streaming_supported_count: 1,
      structured_output_supported_count: 1,
      vision_supported_count: 1,
      avg_latency_ms: 185,
      avg_ttft_ms: 68,
      completed_rounds: 2,
      total_rounds: 2,
      message: '共探测 1/1 个模型（2/2 轮，2/2 条用例），可用 1 个',
    },
    summary_text: '共探测 1/1 个模型（2/2 轮，2/2 条用例），可用 1 个',
    started_at: '2026-11-27T10:00:00Z',
    finished_at: '2026-11-27T10:00:05Z',
    created_at: '2026-11-27T10:00:00Z',
    updated_at: '2026-11-27T10:00:05Z',
    discover_meta: null,
    models_snapshot: [profile],
    rounds: [],
    sub_cases: [
      {
        case_id: 'r1:gpt-4o',
        round_index: 1,
        case_index: 0,
        label: '第 1 轮 · gpt-4o',
        model_id: 'gpt-4o',
        checks: ['availability', 'tool_calling', 'streaming'],
        status: 'available',
        latency_ms: 185,
        ttft_ms: 68,
        request_snapshot: {
          base_url: 'https://api.example.com/v1',
          protocol: 'openai',
          endpoint_path: '/chat/completions',
          model_id: 'gpt-4o',
          checks: ['availability', 'tool_calling', 'streaming'],
          timeout_seconds: 20,
          round_index: 1,
        },
        observation: { available: true, latency_ms: 185 },
        profile,
        created_at: '2026-11-27T10:00:01Z',
      },
    ],
    ...overrides,
  }
}

test('ModelTestingView mounts inside TestWorkspace(results-max=1400) with HistoryList, collapsed settings, TaskChat(model_testing) and never cancels runs on unmount', () => {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/views/ModelTestingView.vue'), 'utf8')
  assert.match(sfc, /<TestWorkspace[\s\S]*?:results-max="1400"/)
  assert.match(sfc, /<HistoryList[\s\S]*?:active-id="activeHistoryId"/)
  assert.match(sfc, /<TaskChat[\s\S]*?scope="model_testing"/)
  assert.match(sfc, /<details[^>]*>\s*<summary>设置<\/summary>[\s\S]*?<TestSettingsFields\s+group="model_testing"/)

  const unmountBlock = sfc.slice(sfc.indexOf('onBeforeUnmount('))
  const unmountHandler = unmountBlock.slice(0, unmountBlock.indexOf('</script>'))
  assert.doesNotMatch(unmountHandler, /stopProbing/)
  assert.doesNotMatch(unmountHandler, /cancelModelTestRun/)
})

test('modelTestingWorkbench store preserves form, memory-only apiKey, discovered models and replay state across route switches without touching localStorage', () => {
  const storeSource = fs.readFileSync(path.join(__dirname, '../src/stores/modelTestingWorkbench.ts'), 'utf8')
  const storeCodeWithoutComments = storeSource
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/[^\n]*/g, '')
  assert.doesNotMatch(storeCodeWithoutComments, /localStorage|sessionStorage/)

  const appPinia = pinia.createPinia()
  pinia.setActivePinia(appPinia)
  const { useWorkbenchRunsStore, useModelTestingWorkbenchStore } = loadStores()
  const storage = memoryStorage()

  const first = loadView(
    'ModelTestingView',
    ['form', 'urlPrefix', 'models', 'selectedIds', 'activeHistoryId', 'assistantContext', 'ephemeralApiKeyForAssistant'],
    {
      storage,
      useWorkbenchRunsStore,
      useModelTestingWorkbenchStore,
      getLLMConfig: async () => null,
      listModelTestRuns: async () => ({ items: [], page: { limit: 50, offset: 0, total: 0 } }),
      getActiveModelTestRuns: async () => ({ items: [] }),
    },
  )

  first.urlPrefix.value = 'https://'
  first.form.baseUrl = 'proxy.example.com/v1'
  first.form.apiKey = 'sk-ephemeral-memory-only-secret'
  first.form.protocol = 'anthropic'
  first.models.value = [sampleProfile('claude-sonnet-4-5')]
  first.selectedIds.value = ['claude-sonnet-4-5']

  // Context snapshot NEVER leaks raw apiKey, while ephemeral prop carries it in memory
  assert.ok(!JSON.stringify(first.assistantContext.value).includes('sk-ephemeral-memory-only-secret'))
  assert.equal(first.assistantContext.value.config.has_custom_api_key, true)
  assert.equal(first.ephemeralApiKeyForAssistant.value, 'sk-ephemeral-memory-only-secret')

  // Simulate route switch (unmount -> remount)
  first.dispose()

  // Verify localStorage NEVER contains the apiKey
  assert.ok(!JSON.stringify(storage.dump()).includes('sk-ephemeral-memory-only-secret'))

  const second = loadView(
    'ModelTestingView',
    ['form', 'urlPrefix', 'models', 'selectedIds'],
    {
      storage,
      useWorkbenchRunsStore,
      useModelTestingWorkbenchStore,
      getLLMConfig: async () => null,
      listModelTestRuns: async () => ({ items: [], page: { limit: 50, offset: 0, total: 0 } }),
      getActiveModelTestRuns: async () => ({ items: [] }),
    },
  )
  assert.equal(second.urlPrefix.value, 'https://')
  assert.equal(second.form.baseUrl, 'proxy.example.com/v1')
  assert.equal(second.form.apiKey, 'sk-ephemeral-memory-only-secret')
  assert.equal(second.form.protocol, 'anthropic')
  assert.deepEqual(second.models.value.map(m => m.id), ['claude-sonnet-4-5'])
  assert.deepEqual(second.selectedIds.value, ['claude-sonnet-4-5'])
  second.dispose()
})

test('ModelTestingView runs persistent probes via startModelTestRun + followModelTestRun, survives unmount without cancelling, auto-reconnects on mount, and cancels only on explicit stopProbing', async context => {
  const appPinia = pinia.createPinia()
  pinia.setActivePinia(appPinia)
  const { useWorkbenchRunsStore, useModelTestingWorkbenchStore } = loadStores()

  const startedPayloads = []
  const cancelledIds = []
  const followCalls = []
  let firstGate = deferred()
  let firstSignal = null
  let secondGate = deferred()
  let secondOnFrame = null

  const runInitial = sampleRunDetail('run-live-1', {
    source: 'manual',
    badge: null,
    status: 'running',
    total_rounds: 1,
    completed_rounds: 0,
    sub_cases: [],
  })

  const sharedMocks = {
    useWorkbenchRunsStore,
    useModelTestingWorkbenchStore,
    getLLMConfig: async () => null,
    listModelTestRuns: async () => ({ items: [runInitial], page: { limit: 50, offset: 0, total: 1 } }),
    startModelTestRun: async body => {
      startedPayloads.push(body)
      return runInitial
    },
    followModelTestRun: (runId, signal, onFrame) => {
      followCalls.push({ runId, signal })
      if (followCalls.length === 1) {
        firstSignal = signal
        onFrame({
          type: 'progress',
          run_id: runId,
          title: runInitial.title,
          source: 'manual',
          badge: null,
          status: 'running',
          stage: 'executing',
          base_url: runInitial.base_url,
          protocol: 'openai',
          resolved_protocol: 'openai',
          current_round: 1,
          total_rounds: 1,
          completed_rounds: 0,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          current_models: ['gpt-4o'],
          completed_cases: 0,
          total_cases: 1,
          models_snapshot: [sampleProfile('gpt-4o', { availability: { status: 'untested', source: 'untested', latency_ms: null, detail: null } })],
          new_sub_cases: [],
          summary: null,
          summary_text: null,
          error_message: null,
        })
        return firstGate.promise
      }
      secondOnFrame = onFrame
      return secondGate.promise
    },
    getActiveModelTestRuns: async () => ({
      items: [
        {
          id: 'run-live-1',
          title: runInitial.title,
          source: 'manual',
          badge: null,
          status: 'running',
          stage: 'cooling_down',
          base_url: runInitial.base_url,
          protocol: 'openai',
          resolved_protocol: 'openai',
          target_models: ['gpt-4o'],
          checks: ['availability', 'tool_calling'],
          current_round: 1,
          total_rounds: 2,
          completed_rounds: 1,
          cooldown_ms: 500,
          cooldown_remaining_ms: 300,
          current_models: [],
          completed_cases: 1,
          total_cases: 2,
          started_at: '2026-11-27T10:00:00Z',
          created_at: '2026-11-27T10:00:00Z',
        },
      ],
    }),
    cancelModelTestRun: async id => {
      cancelledIds.push(id)
      return { ...runInitial, status: 'cancelled' }
    },
  }

  const page1 = loadView(
    'ModelTestingView',
    ['form', 'models', 'selectedIds', 'activeRunId', 'activeHistoryId', 'probing', 'runProbe'],
    sharedMocks,
  )
  page1.form.baseUrl = 'https://api.example.com/v1'
  page1.models.value = [sampleProfile('gpt-4o', { availability: { status: 'untested', source: 'untested', latency_ms: null, detail: null } })]
  page1.selectedIds.value = ['gpt-4o']

  const runPromise = page1.runProbe(['gpt-4o'])
  await vue.nextTick()
  assert.equal(startedPayloads.length, 1)
  assert.equal(startedPayloads[0].cooldown_ms, 500)
  assert.equal(page1.activeRunId.value, 'run-live-1')
  assert.equal(page1.probing.value, true)

  // Unmount (route switch) MUST NOT cancel backend run; only aborts local observer
  page1.dispose()
  firstGate.reject(new DOMException('Aborted', 'AbortError'))
  await runPromise
  assert.equal(firstSignal.aborted, true)
  assert.deepEqual(cancelledIds, [])
  assert.equal(useWorkbenchRunsStore().activeRunId.model_testing, 'run-live-1')

  // Remount auto-reconnects to active run and reflects cooling_down stage
  const page2 = loadView(
    'ModelTestingView',
    ['activeRunId', 'liveStage', 'probing', 'stopProbing'],
    sharedMocks,
  )
  context.after(page2.dispose)
  await page2.mount()
  assert.equal(followCalls.length, 2)
  assert.equal(followCalls[1].runId, 'run-live-1')
  assert.equal(page2.liveStage.value, 'cooling_down')
  assert.equal(page2.probing.value, true)

  secondOnFrame({
    type: 'cooling_down',
    run_id: 'run-live-1',
    title: runInitial.title,
    source: 'manual',
    badge: null,
    status: 'running',
    stage: 'cooling_down',
    base_url: runInitial.base_url,
    protocol: 'openai',
    resolved_protocol: 'openai',
    current_round: 1,
    total_rounds: 2,
    completed_rounds: 1,
    cooldown_ms: 500,
    cooldown_remaining_ms: 200,
    current_models: [],
    completed_cases: 1,
    total_cases: 2,
    models_snapshot: [sampleProfile('gpt-4o')],
    new_sub_cases: [],
    summary: null,
    summary_text: null,
    error_message: null,
  })
  assert.match(await page2.render(), /轮间冷却中/)

  // Explicit stop button calls cancelModelTestRun
  await page2.stopProbing()
  assert.deepEqual(cancelledIds, ['run-live-1'])
  assert.equal(page2.activeRunId.value, null)
  assert.equal(useWorkbenchRunsStore().activeRunId.model_testing, null)
})

test('ModelTestingView supports @run-bound live reattach, @select-case readonly replay lock, 再次测试 unlock+draftVersion++, and 409 delete protection', async context => {
  const appPinia = pinia.createPinia()
  pinia.setActivePinia(appPinia)
  const { useWorkbenchRunsStore, useModelTestingWorkbenchStore } = loadStores()

  const aiRun = sampleRunDetail('ai-run-42')
  const followed = []
  const toasts = []
  let subCaseRequested = null

  const page = loadView(
    'ModelTestingView',
    [
      'form', 'models', 'activeHistoryId', 'activeRunId', 'draftVersion', 'isReadOnlyReplay',
      'readonlyReplay', 'historyEntries', 'handleRunBound', 'handleRunCompleted', 'handleSelectCase',
      'retestCurrent', 'handleDeleteHistory', 'discover', 'runProbe',
    ],
    {
      useWorkbenchRunsStore,
      useModelTestingWorkbenchStore,
      useToastStore: () => ({ push: (...args) => toasts.push(args) }),
      getLLMConfig: async () => null,
      listModelTestRuns: async () => ({ items: [aiRun], page: { limit: 50, offset: 0, total: 1 } }),
      getModelTestRun: async () => aiRun,
      getModelTestSubCase: async (runId, caseId) => {
        subCaseRequested = { runId, caseId }
        return aiRun.sub_cases[0]
      },
      followModelTestRun: async (runId, _signal, onFrame) => {
        followed.push(runId)
        const done = {
          type: 'done',
          run_id: runId,
          title: aiRun.title,
          source: 'ai',
          badge: 'AI 测试',
          status: 'completed',
          stage: 'finished',
          base_url: aiRun.base_url,
          protocol: 'openai',
          resolved_protocol: 'openai',
          current_round: 2,
          total_rounds: 2,
          completed_rounds: 2,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          current_models: [],
          completed_cases: 2,
          total_cases: 2,
          models_snapshot: aiRun.models_snapshot,
          new_sub_cases: aiRun.sub_cases,
          summary: aiRun.summary,
          summary_text: aiRun.summary_text,
          error_message: null,
          run: aiRun,
        }
        onFrame(done)
        return done
      },
      deleteModelTestRun: async () => {
        throw new clientExports.ApiError(409, 'model_test_run_active', 'Run is active')
      },
      discoverProviderModels: async () => {
        throw new Error('discover must not execute in readonly replay')
      },
      startModelTestRun: async () => {
        throw new Error('startModelTestRun must not execute in readonly replay')
      },
    },
  )
  context.after(page.dispose)

  // @run-bound refreshes history and attaches live observer
  await page.handleRunBound({
    scope: 'model_testing',
    history_id: 'ai-run-42',
    previous_history_id: null,
    badge: 'AI 测试',
    title: aiRun.title,
    forked: false,
  })
  await Promise.resolve()
  assert.deepEqual(followed, ['ai-run-42'])
  assert.equal(page.activeHistoryId.value, 'ai-run-42')
  assert.equal(page.historyEntries.value[0].badges[0].label, 'AI 测试')

  await page.handleRunCompleted({ scope: 'model_testing', historyId: 'ai-run-42' })
  assert.equal(page.models.value.length, 1)

  // @select-case enters read-only replay lock mode
  await page.handleSelectCase({
    type: 'case_link',
    scope: 'model_testing',
    history_id: 'ai-run-42',
    case_id: 'r1:gpt-4o',
    round: 1,
    label: '第 1 轮 · gpt-4o',
    status: 'available',
    tone: 'success',
  })
  assert.equal(page.isReadOnlyReplay.value, true)
  assert.equal(page.readonlyReplay.value.caseId, 'r1:gpt-4o')
  assert.equal(useWorkbenchRunsStore().readonlyReplay.model_testing.caseId, 'r1:gpt-4o')

  // Controls are blocked while in read-only replay
  await page.discover()
  await page.runProbe(['gpt-4o'])

  const lockedHtml = await page.render()
  assert.match(lockedHtml, /只读回放/)
  assert.match(lockedHtml, /再次测试/)
  assert.match(lockedHtml, /子用例回放快照/)

  // Explicit 再次测试 unlocks controls, clears activeHistoryId/results, and bumps draftVersion++
  const prevDraftVersion = page.draftVersion.value
  page.retestCurrent()
  assert.equal(page.isReadOnlyReplay.value, false)
  assert.equal(page.activeHistoryId.value, null)
  assert.equal(page.activeRunId.value, null)
  assert.equal(page.models.value.length, 0)
  assert.equal(page.draftVersion.value, prevDraftVersion + 1)
  assert.equal(useWorkbenchRunsStore().readonlyReplay.model_testing, null)
  // Base URL kept unlocked for quick re-test
  assert.equal(page.form.baseUrl, 'https://api.example.com/v1')

  // 409 active delete shows graceful warning toast
  await page.handleDeleteHistory('ai-run-42')
  assert.equal(toasts.at(-1)[0], 'warning')
  assert.match(toasts.at(-1)[1], /无法删除运行中的任务/)

  // Also test getModelTestSubCase fetch when switching to another run's case
  await page.handleSelectCase({
    type: 'case_link',
    scope: 'model_testing',
    history_id: 'other-run-99',
    case_id: 'r1:gpt-4o',
    round: 1,
    label: '第 1 轮 · gpt-4o',
    status: 'available',
    tone: 'success',
  })
  assert.deepEqual(subCaseRequested, { runId: 'other-run-99', caseId: 'r1:gpt-4o' })
})

test('HistoryView renders all 4 tabs including 大模型测试, displays AI badges across all 4 tabs, and opens model test in workbench', async context => {
  const appPinia = pinia.createPinia()
  pinia.setActivePinia(appPinia)
  const { useWorkbenchRunsStore } = loadStores()

  const navigated = []
  const deleted = []
  let clearedModels = 0
  const modelDetail = sampleRunDetail('mt-1')

  const page = loadView(
    'HistoryView',
    [
      'tab', 'httpItems', 'streamItems', 'taskItems', 'modelTestItems',
      'filteredModelTestItems', 'selectedModelTest', 'searchQuery', 'statusFilter',
      'load', 'selectModelTest', 'removeModelTestRecord', 'clearModelTests', 'openModelTestInWorkbench',
    ],
    {
      useWorkbenchRunsStore,
      useRouter: () => ({ push: target => navigated.push(target) }),
      listConsoleHistory: async () => ({
        items: [{
          id: 'h-ai',
          method: 'POST',
          url: 'http://127.0.0.1:8080/api/orders',
          title: 'AI 订单边界测试',
          source: 'ai',
          badge: 'AI 测试',
          request_headers: {},
          body_mode: 'json',
          body_preview: null,
          ok: true,
          status_code: 200,
          elapsed_ms: 42,
          error_kind: null,
          error_message: null,
          response_headers: {},
          response_preview: '{}',
          response_bytes: 2,
          created_at: '2026-11-27T10:00:00Z',
        }],
      }),
      listStreams: async () => ({
        items: [{
          id: 's-ai',
          kind: 'websocket',
          url: 'ws://127.0.0.1:8080/ws',
          title: 'AI 心跳压测',
          source: 'ai',
          badge: null,
          request_headers: {},
          status: 'closed',
          stop_reason: 'client_stopped',
          error_kind: null,
          error_message: null,
          http_status: 101,
          content_type: null,
          subprotocol: null,
          close_code: 1000,
          close_reason: null,
          event_count: 4,
          byte_count: 120,
          sent_count: 2,
          truncated_count: 0,
          settings: {},
          opened_at: '2026-11-27T10:00:00Z',
          closed_at: '2026-11-27T10:00:02Z',
          created_at: '2026-11-27T10:00:00Z',
        }],
      }),
      listTasks: async () => ({
        items: [{
          id: 't-ai',
          title: 'AI 群测回归',
          origin: 'http://127.0.0.1:8080',
          source: 'ai',
          badge: 'AI 测试',
          status: 'completed',
          stage: 'finished',
          created_at: '2026-11-27T10:00:00Z',
          error_kind: null,
        }],
      }),
      listModelTestRuns: async () => ({
        items: [modelDetail],
        page: { limit: 60, offset: 0, total: 1 },
      }),
      getModelTestRun: async () => modelDetail,
      deleteModelTestRun: async id => { deleted.push(id) },
      clearModelTestRuns: async () => { clearedModels++ },
    },
  )
  context.after(page.dispose)

  await page.load()
  assert.equal(page.httpItems.value.length, 1)
  assert.equal(page.streamItems.value.length, 1)
  assert.equal(page.taskItems.value.length, 1)
  assert.equal(page.modelTestItems.value.length, 1)

  // Verify 4 tabs and AI badge on HTTP tab
  const httpHtml = await page.render()
  assert.match(httpHtml, /HTTP 请求 1/)
  assert.match(httpHtml, /长连接会话 1/)
  assert.match(httpHtml, /智能群测 1/)
  assert.match(httpHtml, /大模型测试 1/)
  assert.match(httpHtml, /ai-badge[^>]*>AI 测试/)

  // Verify AI badge on Stream tab (derived from source === 'ai')
  page.tab.value = 'stream'
  await vue.nextTick()
  assert.match(await page.render(), /ai-badge[^>]*>AI 测试/)

  // Verify AI badge on Task tab
  page.tab.value = 'task'
  await vue.nextTick()
  assert.match(await page.render(), /ai-badge[^>]*>AI 测试/)

  // Verify 4th tab 大模型测试: filter, detail, AI badge, and open in workbench
  page.tab.value = 'model_testing'
  await vue.nextTick()
  assert.equal(page.filteredModelTestItems.value.length, 1)
  page.searchQuery.value = 'gpt-4o'
  assert.equal(page.filteredModelTestItems.value.length, 1)
  page.statusFilter.value = 'error'
  assert.equal(page.filteredModelTestItems.value.length, 0)
  page.statusFilter.value = 'completed'
  assert.equal(page.filteredModelTestItems.value.length, 1)

  await page.selectModelTest('mt-1')
  assert.equal(page.selectedModelTest.value.id, 'mt-1')
  const modelHtml = await page.render()
  assert.match(modelHtml, /ai-badge[^>]*>AI 测试/)
  assert.match(modelHtml, /在工作台打开/)
  assert.match(modelHtml, /gpt-4o/)
  assert.match(modelHtml, /TTFT 68ms/)

  page.openModelTestInWorkbench('mt-1')
  assert.equal(useWorkbenchRunsStore().activeHistoryId.model_testing, 'mt-1')
  assert.deepEqual(navigated[0], { path: '/model-testing', query: { historyId: 'mt-1' } })

  // Delete and clear work cleanly
  await page.removeModelTestRecord('mt-1')
  assert.deepEqual(deleted, ['mt-1'])
  assert.equal(page.modelTestItems.value.length, 0)

  page.modelTestItems.value = [modelDetail]
  await page.clearModelTests()
  assert.equal(clearedModels, 1)
  assert.equal(page.modelTestItems.value.length, 0)
})
