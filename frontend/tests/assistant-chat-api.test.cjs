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

const settle = async (ticks = 8) => {
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

function loadChatHistory(storage = memoryStorage()) {
  return loadTsModule('src/lib/chatHistory.ts', {
    window: { localStorage: storage },
  })
}

function loadEndpoints(mockFetch) {
  const timers = new Map()
  let nextId = 0
  const mockWindow = {
    setTimeout: fn => { timers.set(++nextId, fn); return nextId },
    clearTimeout: id => timers.delete(id),
  }
  const clientMod = loadTsModule('src/api/client.ts', {
    window: mockWindow,
    fetch: mockFetch,
  })
  const endpointsMod = loadTsModule('src/api/endpoints.ts', {
    require: id => {
      if (id === './client') return clientMod
      throw new Error(`Unexpected require in endpoints.ts: ${id}`)
    },
  })
  return { client: clientMod, endpoints: endpointsMod, timers }
}

function mountTaskChat(props = {}, mocks = {}) {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/components/TaskChat.vue'), 'utf8')
  const script = sfc
    .split('<script setup lang="ts">')[1]
    .split('</script>')[0]
    .replace(/^\s*import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/gm, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText

  const storage = mocks.storage ?? memoryStorage()
  const chatHistoryMod = loadChatHistory(storage)
  const copyMod = loadTsModule('src/lib/copy.ts')
  const clientMod = loadTsModule('src/api/client.ts', {
    window: { setTimeout: () => 1, clearTimeout: () => {} },
    fetch: () => { throw new Error('Unexpected fetch') },
  })
  const storeMod = loadTsModule('src/stores/workbenchRuns.ts', {
    require: id => {
      if (id === 'pinia') return pinia
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
  })
  const appPinia = mocks.pinia ?? pinia.createPinia()
  pinia.setActivePinia(appPinia)

  const reactiveProps = vue.reactive({ scope: 'console', ...props })
  const emitted = []
  const mounted = []
  const unmounted = []
  const scope = vue.effectScope()

  const env = {
    ...vue,
    ...copyMod,
    ...chatHistoryMod,
    ApiError: clientMod.ApiError,
    describeError: clientMod.describeError,
    useWorkbenchRunsStore: storeMod.useWorkbenchRunsStore,
    defineProps: () => reactiveProps,
    defineEmits: () => (event, ...args) => emitted.push([event, ...args]),
    onMounted: fn => mounted.push(fn),
    onBeforeUnmount: fn => unmounted.push(fn),
    useId: () => 'task-chat-test-menu',
    getLLMConfig: async () => ({ model: 'gpt-4o' }),
    getLLMModels: async () => ({ ok: true, models: ['gpt-4o'] }),
    selectLLMModel: async model => ({ model }),
    window: {
      localStorage: storage,
      setInterval: () => 1,
      clearInterval: () => {},
      setTimeout: () => 1,
      clearTimeout: () => {},
    },
    ...mocks,
  }

  const bindings = []
  function collect(node) {
    if (ts.isIdentifier(node)) bindings.push(node.text)
    else node.elements.forEach(entry => { if (!ts.isOmittedExpression(entry)) collect(entry.name) })
  }
  for (const stmt of ts.createSourceFile('TaskChat.vue', script, ts.ScriptTarget.Latest, true).statements) {
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
    props: reactiveProps,
    emitted,
    storage,
    ApiError: clientMod.ApiError,
    store: storeMod.useWorkbenchRunsStore(),
    pinia: appPinia,
    triggerMounted: () => mounted.forEach(fn => fn()),
    unmount: () => { unmounted.forEach(fn => fn()); scope.stop() },
    render: async () => {
      const ctx = vue.proxyRefs({
        ...env,
        ...reactiveProps,
        $slots: {},
        ...Object.fromEntries(iconNames.map(icon => [icon, {}])),
        ...state,
      })
      const app = vue.createSSRApp({ render: () => render(ctx, []) })
      app.component('AiMarkdown', { props: ['text'], setup: p => () => vue.h('div', { class: 'ai-md' }, p.text) })
      app.component('AssistantAvatar', { props: ['size', 'state'], setup: p => () => vue.h('span', { class: `avatar-${p.state}` }) })
      for (const icon of iconNames) app.component(icon, { render: () => null })
      return renderToString(app)
    },
  }
}

test('CaseLinkBlock normalization and deduplication validate scopes, tones, rounds, and preserve order', () => {
  const { normalizeCaseLinkBlock, normalizeCaseLinkBlocks, dedupeCaseLinkBlocks } = loadChatHistory()

  // Rejects malformed entries
  assert.equal(normalizeCaseLinkBlock(null), null)
  assert.equal(normalizeCaseLinkBlock({ type: 'other', scope: 'console', history_id: 'h1', case_id: 'c1' }), null)
  assert.equal(normalizeCaseLinkBlock({ type: 'case_link', scope: 'invalid', history_id: 'h1', case_id: 'c1' }), null)
  assert.equal(normalizeCaseLinkBlock({ type: 'case_link', scope: 'console', history_id: '  ', case_id: 'c1' }), null)
  assert.equal(normalizeCaseLinkBlock({ type: 'case_link', scope: 'console', history_id: 'h1', case_id: '' }), null)

  // Normalizes tone aliases ('error' -> 'danger', 'passed' -> 'success') and round defaults
  const normalized = normalizeCaseLinkBlock({
    type: 'case_link',
    scope: 'model_testing',
    history_id: ' run-9 ',
    case_id: ' r2:gpt-4o ',
    round: 2.8,
    label: ' 第 2 轮 · gpt-4o ',
    status: 'unavailable',
    tone: 'error',
  })
  assert.deepEqual(normalized, {
    type: 'case_link',
    scope: 'model_testing',
    history_id: 'run-9',
    case_id: 'r2:gpt-4o',
    round: 2,
    label: '第 2 轮 · gpt-4o',
    status: 'unavailable',
    tone: 'danger',
  })

  const fallbackBlock = normalizeCaseLinkBlock({
    type: 'case_link',
    scope: 'console',
    history_id: 'h1',
    case_id: 'c1',
    round: 0,
    label: '',
    status: '',
    tone: 'unknown-tone',
  })
  assert.deepEqual(fallbackBlock, {
    type: 'case_link',
    scope: 'console',
    history_id: 'h1',
    case_id: 'c1',
    round: 1,
    label: 'c1',
    status: 'completed',
    tone: 'neutral',
  })

  // Batch normalization filters out invalid blocks and deduplication preserves insertion order with latest update
  const blocks = normalizeCaseLinkBlocks([
    { type: 'case_link', scope: 'console', history_id: 'h1', case_id: 'c1', round: 1, label: '空参', status: 'running', tone: 'info' },
    { type: 'text', content: 'ignored' },
    { type: 'case_link', scope: 'console', history_id: 'h1', case_id: 'c2', round: 1, label: '正常', status: 'HTTP 200', tone: 'success' },
    { type: 'case_link', scope: 'console', history_id: 'h1', case_id: 'c1', round: 1, label: '空参', status: 'HTTP 400', tone: 'warning' },
  ])
  assert.equal(blocks.length, 3)

  const deduped = dedupeCaseLinkBlocks(blocks)
  assert.equal(deduped.length, 2)
  assert.equal(deduped[0].case_id, 'c1')
  assert.equal(deduped[0].status, 'HTTP 400')
  assert.equal(deduped[0].tone, 'warning')
  assert.equal(deduped[1].case_id, 'c2')
})

test('endpoints.ts maps persistent console, stream, model-testing, and assistant routes & NDJSON streams accurately', async () => {
  const calls = []
  const { endpoints } = loadEndpoints(async (url, init = {}) => {
    calls.push({ url, method: init.method ?? 'GET', body: init.body ? JSON.parse(init.body) : undefined })
    if (url.endsWith('/live')) {
      if (url.startsWith('/api/assistant/runs/')) {
        const ndjson = [
          JSON.stringify({ type: 'status', phase: 'testing', message: '执行中' }),
          JSON.stringify({
            type: 'case_link',
            block: {
              type: 'case_link',
              scope: 'console',
              history_id: 'h-new',
              case_id: 'sc-1',
              round: 1,
              label: '边界用例',
              status: 'HTTP 200',
              tone: 'success',
            },
          }),
          JSON.stringify({ type: 'text', text: '测试完成' }),
          JSON.stringify({ type: 'done', text: '测试完成', history_id: 'h-new', blocks: [] }),
        ].join('\n') + '\n'
        return new Response(ndjson, { headers: { 'Content-Type': 'application/x-ndjson' } })
      }
      if (url.startsWith('/api/console/runs/')) {
        const ndjson = JSON.stringify({ type: 'done', run_id: 'cr-1', status: 'completed', ok: true }) + '\n'
        return new Response(ndjson, { headers: { 'Content-Type': 'application/x-ndjson' } })
      }
      if (url.startsWith('/api/streams/suites/')) {
        const ndjson = JSON.stringify({ type: 'done', suite_id: 'ss-1', id: 'ss-1', status: 'completed', stage: 'finished', source: 'ai', title: null, badge: 'AI 测试', url: 'ws://127.0.0.1/ws', current_round: 1, total_rounds: 1, completed_cases: 1, total_cases: 1, cooldown_ms: 500, cooldown_remaining_ms: null, summary: 'ok', error_kind: null, error_message: null }) + '\n'
        return new Response(ndjson, { headers: { 'Content-Type': 'application/x-ndjson' } })
      }
      if (url.startsWith('/api/model-testing/runs/')) {
        const ndjson = JSON.stringify({ type: 'done', run_id: 'mt-1', status: 'completed', stage: 'finished' }) + '\n'
        return new Response(ndjson, { headers: { 'Content-Type': 'application/x-ndjson' } })
      }
    }
    if (init.method === 'DELETE') {
      return new Response(null, { status: 204 })
    }
    return new Response(JSON.stringify({ ok: true, items: [], run_id: 'asst-1', scope: 'console', history_id: null, status: 'running' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })

  // Console persistent endpoints
  await endpoints.startConsoleRun({ method: 'GET', url: 'http://127.0.0.1:8000', headers: {}, body_mode: 'none', timeout_ms: 1000, follow_redirects: false })
  await endpoints.getActiveConsoleRuns()
  const consoleFrames = []
  await endpoints.followConsoleRun('cr-1', undefined, frame => consoleFrames.push(frame.type))
  await endpoints.cancelConsoleRun('cr-1')
  await endpoints.getConsoleSubCase('rec-1', 'sub-1')

  // Streams active / sub-cases / AI suite endpoints
  await endpoints.getActiveStreams()
  await endpoints.listStreamSubCases('ws-1')
  await endpoints.getStreamSubCase('ws-1', 'child-1')
  const suiteFrames = []
  await endpoints.followStreamSuite('ss-1', undefined, frame => suiteFrames.push(frame.type))
  await endpoints.cancelStreamSuite('ss-1')

  // Model testing persistent endpoints (including path-encoded case_id with slash)
  await endpoints.startModelTestRun({ models: ['gpt-4o'], rounds: 2, cooldown_ms: 500 })
  await endpoints.getActiveModelTestRuns()
  await endpoints.listModelTestRuns(20, 0)
  await endpoints.getModelTestRun('mt-1')
  await endpoints.getModelTestSubCase('mt-1', 'r1:openai/gpt-4o')
  const modelFrames = []
  await endpoints.followModelTestRun('mt-1', undefined, frame => modelFrames.push(frame.type))
  await endpoints.cancelModelTestRun('mt-1')
  await endpoints.deleteModelTestRun('mt-1')
  await endpoints.clearModelTestRuns()

  // Assistant endpoints
  await endpoints.startAssistantRun({ scope: 'console', history_id: 'h1', messages: [{ role: 'user', content: '测试一下' }] })
  await endpoints.getActiveAssistantRuns('console')
  const assistantFrames = []
  const assistantDone = await endpoints.followAssistantRun('asst-1', undefined, frame => assistantFrames.push(frame.type))
  await endpoints.cancelAssistantRun('asst-1')
  await endpoints.getAssistantThread('console', 'h1')
  await endpoints.migrateAssistantThread('console', 'h1', { messages: [{ role: 'user', content: 'hi' }] })
  await endpoints.deleteAssistantThread('console', 'h1')

  assert.deepEqual(consoleFrames, ['done'])
  assert.deepEqual(suiteFrames, ['done'])
  assert.deepEqual(modelFrames, ['done'])
  assert.deepEqual(assistantFrames, ['status', 'case_link', 'text', 'done'])
  assert.equal(assistantDone.history_id, 'h-new')

  const requestedPaths = calls.map(c => `${c.method} ${c.url}`)
  assert.ok(requestedPaths.includes('POST /api/console/runs'))
  assert.ok(requestedPaths.includes('GET /api/console/runs/active'))
  assert.ok(requestedPaths.includes('GET /api/console/runs/cr-1/live'))
  assert.ok(requestedPaths.includes('POST /api/console/runs/cr-1/cancel'))
  assert.ok(requestedPaths.includes('GET /api/console/history/rec-1/cases/sub-1'))
  assert.ok(requestedPaths.includes('GET /api/streams/active'))
  assert.ok(requestedPaths.includes('GET /api/streams/ws-1/cases'))
  assert.ok(requestedPaths.includes('GET /api/streams/ws-1/cases/child-1'))
  assert.ok(requestedPaths.includes('GET /api/streams/suites/ss-1/live'))
  assert.ok(requestedPaths.includes('POST /api/streams/suites/ss-1/cancel'))
  assert.ok(requestedPaths.includes('POST /api/model-testing/runs'))
  assert.ok(requestedPaths.includes('GET /api/model-testing/runs/active'))
  assert.ok(requestedPaths.includes('GET /api/model-testing/runs?limit=20&offset=0'))
  assert.ok(requestedPaths.includes('GET /api/model-testing/runs/mt-1'))
  assert.ok(requestedPaths.includes('GET /api/model-testing/runs/mt-1/cases/r1%3Aopenai/gpt-4o'))
  assert.ok(requestedPaths.includes('GET /api/model-testing/runs/mt-1/live'))
  assert.ok(requestedPaths.includes('POST /api/model-testing/runs/mt-1/cancel'))
  assert.ok(requestedPaths.includes('DELETE /api/model-testing/runs/mt-1'))
  assert.ok(requestedPaths.includes('DELETE /api/model-testing/runs'))
  assert.ok(requestedPaths.includes('POST /api/assistant/runs'))
  assert.ok(requestedPaths.includes('GET /api/assistant/runs/active?scope=console'))
  assert.ok(requestedPaths.includes('GET /api/assistant/runs/asst-1/live'))
  assert.ok(requestedPaths.includes('POST /api/assistant/runs/asst-1/cancel'))
  assert.ok(requestedPaths.includes('GET /api/assistant/threads/console/h1'))
  assert.ok(requestedPaths.includes('PUT /api/assistant/threads/console/h1/migrate'))
  assert.ok(requestedPaths.includes('DELETE /api/assistant/threads/console/h1'))
})

test('TaskChat atomic fork via run_bound isolates new run conversation, streams clickable CaseLinkBlock chips, and emits events', async context => {
  const streamGate = deferred()
  let emitFrame = null
  const storage = memoryStorage({
    'api-agent.chat.console.old-hist': JSON.stringify([
      { role: 'user', content: '旧历史的问题' },
      { role: 'assistant', content: '旧历史的回复' },
    ]),
  })

  const chat = mountTaskChat(
    { scope: 'console', executionId: 'old-hist', activeCaseId: 'sc-2' },
    {
      storage,
      getAssistantThread: async (_scope, historyId) => ({
        id: historyId === 'old-hist' ? 'thr-old' : null,
        scope: 'console',
        history_id: historyId,
        title: null,
        active_run_id: null,
        messages:
          historyId === 'old-hist'
            ? [
                { id: 'm1', role: 'user', content: '旧历史的问题', blocks: [] },
                { id: 'm2', role: 'assistant', content: '旧历史的回复', blocks: [] },
              ]
            : [],
      }),
      getActiveAssistantRuns: async () => ({ items: [] }),
      startAssistantRun: async () => ({
        run_id: 'asst-run-fork',
        scope: 'console',
        history_id: 'old-hist',
        status: 'running',
      }),
      followAssistantRun: (_runId, _signal, onFrame) => {
        emitFrame = onFrame
        return streamGate.promise
      },
      cancelAssistantRun: async () => ({ run_id: 'asst-run-fork', status: 'cancelled' }),
    },
  )
  context.after(chat.unmount)
  await settle()

  assert.equal(chat.state.turns.value.length, 2)

  // Send a new prompt that triggers an AI test and forks away from old-hist
  chat.state.input.value = '帮我跑 2 轮边界测试'
  const sendPromise = chat.state.send()
  await settle()

  // Optimistic prompt must NOT pollute old-hist localStorage before fork decision
  assert.equal(JSON.parse(storage.getItem('api-agent.chat.console.old-hist')).length, 2)
  assert.equal(chat.store.activeAssistantRunId.console, 'asst-run-fork')

  // Backend emits testing status & atomic fork run_bound -> new-hist
  emitFrame({ type: 'status', phase: 'testing' })
  assert.equal(chat.state.statusLabel.value, '正在执行 AI 测试')
  assert.equal(chat.state.avatarState.value, 'reading')

  emitFrame({ type: 'status', phase: 'cooling_down' })
  assert.equal(chat.state.statusLabel.value, '轮间冷却中')
  assert.equal(chat.state.avatarState.value, 'thinking')

  emitFrame({
    type: 'run_bound',
    scope: 'console',
    history_id: 'new-hist',
    previous_history_id: 'old-hist',
    badge: 'AI 测试',
    title: 'AI 边界测试',
    forked: true,
    run_id: 'asst-run-fork',
  })

  // Atomic fork MUST discard old-hist turns and keep ONLY the triggering user prompt!
  assert.deepEqual(
    chat.state.turns.value.map(t => t.content),
    ['帮我跑 2 轮边界测试'],
  )
  const boundEvent = chat.emitted.find(([name]) => name === 'run-bound')
  assert.ok(boundEvent)
  assert.equal(boundEvent[1].history_id, 'new-hist')
  assert.equal(boundEvent[1].previous_history_id, 'old-hist')
  assert.equal(boundEvent[1].forked, true)

  // Live CaseLinkBlocks appear as clickable buttons even before done
  emitFrame({
    type: 'case_link',
    block: {
      type: 'case_link',
      scope: 'console',
      history_id: 'new-hist',
      case_id: 'sc-1',
      round: 1,
      label: '空参边界',
      status: 'HTTP 400',
      tone: 'warning',
    },
  })
  emitFrame({
    type: 'case_link',
    block: {
      type: 'case_link',
      scope: 'console',
      history_id: 'new-hist',
      case_id: 'sc-2',
      round: 2,
      label: '正常请求',
      status: 'HTTP 200',
      tone: 'success',
    },
  })

  const liveHtml = await chat.render()
  assert.match(liveHtml, /class="case-link-list"/)
  assert.match(liveHtml, /class="case-link-chip"[^>]*data-tone="warning"[^>]*data-case-id="sc-1"/)
  assert.match(liveHtml, /class="case-link-chip"[^>]*data-tone="success"[^>]*data-case-id="sc-2"[^>]*aria-current="true"/)
  assert.match(liveHtml, />R1</)
  assert.match(liveHtml, />R2</)
  assert.match(liveHtml, /chat-cancel/)

  // Complete the run
  streamGate.resolve({
    type: 'done',
    text: '2 轮测试全部完成。',
    history_id: 'new-hist',
    blocks: [
      {
        type: 'case_link',
        scope: 'console',
        history_id: 'new-hist',
        case_id: 'sc-1',
        round: 1,
        label: '空参边界',
        status: 'HTTP 400',
        tone: 'warning',
      },
    ],
  })
  await sendPromise

  assert.equal(chat.state.turns.value.length, 2)
  assert.equal(chat.state.turns.value[0].content, '帮我跑 2 轮边界测试')
  assert.equal(chat.state.turns.value[1].content, '2 轮测试全部完成。')
  assert.equal(chat.state.turns.value[1].blocks.length, 2)
  assert.equal(chat.store.activeAssistantRunId.console, null)

  const doneEvent = chat.emitted.find(([name]) => name === 'run-completed')
  assert.deepEqual(doneEvent, ['run-completed', { scope: 'console', historyId: 'new-hist' }])

  // Persisted CaseLinkBlocks remain rendered after completion
  const finalHtml = await chat.render()
  assert.match(finalHtml, /2 轮测试全部完成。/)
  assert.match(finalHtml, /data-case-id="sc-1"/)
  assert.match(finalHtml, /data-case-id="sc-2"/)
})

test('TaskChat unmount NEVER cancels backend run and remount automatically reconnects via workbenchRuns / active runs', async () => {
  const firstStream = deferred()
  const secondStream = deferred()
  let firstSignal = null
  let secondOnFrame = null
  let cancelCalls = 0
  const sharedPinia = pinia.createPinia()

  // First mount starts an assistant run
  const first = mountTaskChat(
    { scope: 'model_testing', executionId: 'mt-1' },
    {
      pinia: sharedPinia,
      getAssistantThread: async () => ({
        id: 'thr-1',
        scope: 'model_testing',
        history_id: 'mt-1',
        title: null,
        active_run_id: null,
        messages: [],
      }),
      getActiveAssistantRuns: async () => ({ items: [] }),
      startAssistantRun: async () => ({
        run_id: 'asst-bg-1',
        scope: 'model_testing',
        history_id: 'mt-1',
        status: 'running',
      }),
      followAssistantRun: (_runId, signal) => {
        firstSignal = signal
        return firstStream.promise
      },
      cancelAssistantRun: async () => {
        cancelCalls++
        return { run_id: 'asst-bg-1', status: 'cancelled' }
      },
    },
  )

  first.state.input.value = '帮我测 3 轮模型可用性'
  const sending = first.state.send()
  await settle()
  assert.equal(first.store.activeAssistantRunId.model_testing, 'asst-bg-1')

  // Simulate navigating to another page: unmount aborts local observer ONLY, NEVER cancels backend run
  first.unmount()
  firstStream.reject(new Error('observer aborted'))
  await sending
  assert.equal(firstSignal.aborted, true)
  assert.equal(cancelCalls, 0)
  assert.equal(first.store.activeAssistantRunId.model_testing, 'asst-bg-1')

  // Second mount (navigating back): auto-reconnects to asst-bg-1 and finishes cleanly
  const followIds = []
  const second = mountTaskChat(
    { scope: 'model_testing', executionId: 'mt-1' },
    {
      pinia: sharedPinia,
      getAssistantThread: async () => ({
        id: 'thr-1',
        scope: 'model_testing',
        history_id: 'mt-1',
        title: null,
        active_run_id: null,
        messages: [{ id: 'u1', role: 'user', content: '帮我测 3 轮模型可用性', blocks: [] }],
      }),
      getActiveAssistantRuns: async scope => {
        assert.equal(scope, 'model_testing')
        return {
          items: [{ run_id: 'asst-bg-1', scope: 'model_testing', history_id: 'mt-1', status: 'running' }],
        }
      },
      startAssistantRun: async () => { throw new Error('Should not start a new run during reconnect') },
      followAssistantRun: (runId, _signal, onFrame) => {
        followIds.push(runId)
        secondOnFrame = onFrame
        return secondStream.promise
      },
      cancelAssistantRun: async () => ({ run_id: 'asst-bg-1', status: 'cancelled' }),
    },
  )
  second.triggerMounted()
  await settle()

  assert.deepEqual(followIds, ['asst-bg-1'])
  assert.equal(second.state.sending.value, true)
  secondOnFrame({ type: 'status', phase: 'testing' })
  assert.equal(second.state.statusLabel.value, '正在执行 AI 测试')
  secondStream.resolve({ type: 'done', text: '重连后收到的最终结论', history_id: 'mt-1', blocks: [] })
  await settle()

  assert.equal(second.state.sending.value, false)
  assert.equal(second.store.activeAssistantRunId.model_testing, null)
  second.unmount()
})

test('TaskChat migrates legacy localStorage transcript when server thread is empty and falls back on 404/405', async context => {
  const migrated = []
  const storage = memoryStorage({
    'api-agent.chat.websocket.ws-old': JSON.stringify([
      { role: 'user', content: '旧 WS 问题' },
      { role: 'assistant', content: '旧 WS 答案' },
    ]),
  })
  const fallbackCalls = []

  const chat = mountTaskChat(
    { scope: 'websocket', executionId: 'ws-old' },
    {
      storage,
      getAssistantThread: async () => ({
        id: null,
        scope: 'websocket',
        history_id: 'ws-old',
        title: null,
        active_run_id: null,
        messages: [],
      }),
      migrateAssistantThread: async (scope, historyId, body) => {
        migrated.push({ scope, historyId, body })
        return { id: 'thr-mig', scope, history_id: historyId, title: null, active_run_id: null, messages: [] }
      },
      getActiveAssistantRuns: async () => ({ items: [] }),
      startAssistantRun: async () => {
        const clientMod = loadTsModule('src/api/client.ts', {
          window: { setTimeout: () => 1, clearTimeout: () => {} },
          fetch: () => {},
        })
        throw new clientMod.ApiError(404, 'not_found', 'Not found')
      },
      followAssistantRun: async () => { throw new Error('Should not follow after 404') },
      chatWithAssistant: async (_taskId, messages) => {
        fallbackCalls.push(messages)
        return '兼容回退回复'
      },
    },
  )
  context.after(chat.unmount)
  await settle()

  // Legacy messages displayed and migrated in background
  assert.equal(chat.state.turns.value.length, 2)
  assert.equal(migrated.length, 1)
  assert.equal(migrated[0].scope, 'websocket')
  assert.equal(migrated[0].historyId, 'ws-old')
  assert.equal(migrated[0].body.messages.length, 2)

  // Sending gracefully falls back to chatWithAssistant when /api/assistant/runs returns 404
  chat.state.input.value = '再问一句'
  await chat.state.send()
  assert.equal(fallbackCalls.length, 1)
  assert.equal(chat.state.turns.value.at(-1).content, '兼容回退回复')
})

test('TaskChat auto-attaches to thread active_run_id when switching history and supports explicit cancelRun', async context => {
  const activeStream = deferred()
  const attachedIds = []
  const cancelledIds = []

  const chat = mountTaskChat(
    { scope: 'tasks', executionId: 'task-idle' },
    {
      getAssistantThread: async (_scope, historyId) => ({
        id: `thr-${historyId}`,
        scope: 'tasks',
        history_id: historyId,
        title: null,
        active_run_id: historyId === 'task-running' ? 'asst-live-9' : null,
        messages:
          historyId === 'task-running'
            ? [{ id: 'u1', role: 'user', content: '帮我执行群测', blocks: [] }]
            : [],
      }),
      getActiveAssistantRuns: async () => ({ items: [] }),
      startAssistantRun: async () => { throw new Error('unexpected start') },
      followAssistantRun: runId => {
        attachedIds.push(runId)
        return activeStream.promise
      },
      cancelAssistantRun: async runId => {
        cancelledIds.push(runId)
        return { run_id: runId, status: 'cancelled' }
      },
    },
  )
  context.after(chat.unmount)
  await settle()

  // Switch to a history item with an active backend assistant run
  chat.props.executionId = 'task-running'
  await vue.nextTick()
  await settle()

  assert.deepEqual(attachedIds, ['asst-live-9'])
  assert.equal(chat.state.sending.value, true)
  assert.equal(chat.state.currentRunId.value, 'asst-live-9')

  // Clicking the explicit stop button invokes cancelAssistantRun
  await chat.state.cancelRun()
  assert.deepEqual(cancelledIds, ['asst-live-9'])

  activeStream.resolve({ type: 'done', text: '已停止，保留部分用例结果。', history_id: 'task-running', blocks: [] })
  await settle()
  assert.equal(chat.state.sending.value, false)
})

test('TaskChat source invariants guard against unmount cancellation and enforce clickable CaseLinkBlock markup', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/components/TaskChat.vue'), 'utf8')
  const unmountBlock = source.slice(source.indexOf('onBeforeUnmount('), source.indexOf('async function loadModels'))
  assert.match(unmountBlock, /observerAbort\.abort\(\)/)
  assert.doesNotMatch(unmountBlock, /cancelAssistantRun/)
  assert.match(source, /emit\('select-case',\s*block\)/)
  assert.match(source, /emit\('run-bound'/)
  assert.match(source, /emit\('run-completed'/)
  assert.match(source, /class="case-link-list"/)
  assert.match(source, /class="case-link-chip"/)
  assert.match(source, /正在执行 AI 测试/)
  assert.match(source, /轮间冷却中/)
})
