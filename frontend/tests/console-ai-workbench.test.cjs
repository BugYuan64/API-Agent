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

function mountRequestConsole(mocks = {}) {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/views/RequestConsole.vue'), 'utf8')
  const script = sfc
    .split('<script setup lang="ts">')[1]
    .split('</script>')[0]
    .replace(/^\s*import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/gm, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText

  const storage = mocks.storage ?? memoryStorage()
  const mockWindow = {
    localStorage: storage,
    addEventListener: () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  }

  const clientMod = loadTsModule('src/api/client.ts', {
    window: mockWindow,
    fetch: () => { throw new Error('Unexpected fetch') },
  })
  const formatMod = loadTsModule('src/lib/format.ts')
  const resultsMod = loadTsModule('src/lib/results.ts')
  const prefixMod = loadTsModule('src/lib/urlPrefixes.ts')
  const copyMod = loadTsModule('src/lib/copy.ts')
  const assistantContextMod = loadTsModule('src/lib/assistantContext.ts')
  const chatHistoryMod = loadTsModule('src/lib/chatHistory.ts', { window: mockWindow })
  const headerRowsMod = loadTsModule('src/composables/useHeaderRows.ts', {
    require: id => {
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
  })
  const testSettingsMod = loadTsModule('src/lib/testSettings.ts', {
    require: id => {
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
    window: mockWindow,
  })
  const workbenchRunsMod = loadTsModule('src/stores/workbenchRuns.ts', {
    require: id => {
      if (id === 'pinia') return pinia
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
  })
  const consoleWorkbenchMod = loadTsModule('src/stores/consoleWorkbench.ts', {
    require: id => {
      if (id === 'pinia') return pinia
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
  })

  const appPinia = mocks.pinia ?? pinia.createPinia()
  pinia.setActivePinia(appPinia)

  const mounted = []
  const unmounted = []
  const toasts = []
  const scope = vue.effectScope()

  const env = {
    ...vue,
    ...formatMod,
    ...resultsMod,
    ...prefixMod,
    ...copyMod,
    ...assistantContextMod,
    ...chatHistoryMod,
    ...headerRowsMod,
    ...testSettingsMod,
    ApiError: clientMod.ApiError,
    describeError: clientMod.describeError,
    useToastStore: () => ({ push: (...args) => toasts.push(args) }),
    useWorkbenchRunsStore: workbenchRunsMod.useWorkbenchRunsStore,
    useConsoleWorkbenchStore: consoleWorkbenchMod.useConsoleWorkbenchStore,
    onMounted: fn => mounted.push(fn),
    onBeforeUnmount: fn => unmounted.push(fn),
    listConsoleHistory: async () => ({ items: [] }),
    readConsoleHistory: async () => { throw new Error('readConsoleHistory not mocked') },
    deleteConsoleHistory: async () => null,
    sendConsoleRequest: async () => ({ ok: true, request: { method: 'GET', url: '' }, response: null, error_kind: null, error_message: null, elapsed_ms: 1 }),
    getActiveConsoleRuns: async () => ({ items: [] }),
    window: mockWindow,
    ...mocks,
  }

  const bindings = []
  function collect(node) {
    if (ts.isIdentifier(node)) bindings.push(node.text)
    else node.elements.forEach(entry => { if (!ts.isOmittedExpression(entry)) collect(entry.name) })
  }
  for (const stmt of ts.createSourceFile('RequestConsole.vue', script, ts.ScriptTarget.Latest, true).statements) {
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
    toasts,
    consoleSettings: testSettingsMod.consoleSettings,
    pinia: appPinia,
    workbenchRuns: workbenchRunsMod.useWorkbenchRunsStore(),
    consoleStore: consoleWorkbenchMod.useConsoleWorkbenchStore(),
    triggerMounted: () => mounted.forEach(fn => fn()),
    unmount: () => { unmounted.forEach(fn => fn()); scope.stop() },
    render: async () => {
      const ctx = vue.proxyRefs({
        ...env,
        $slots: {},
        ...Object.fromEntries(iconNames.map(icon => [icon, {}])),
        ...state,
      })
      const app = vue.createSSRApp({ render: () => render(ctx, []) })
      app.component('UrlInput', {
        props: ['modelValue', 'address', 'label', 'placeholder', 'disabled'],
        setup: (props, { slots }) => () =>
          vue.h('div', { class: 'mock-url-input', 'data-disabled': String(Boolean(props.disabled)) }, [
            ...(slots.leading ? slots.leading() : []),
            vue.h('input', { value: props.address, disabled: Boolean(props.disabled) }),
          ]),
      })
      app.component('TaskChat', {
        props: ['scope', 'executionId', 'draftVersion', 'currentTest', 'settings', 'activeCaseId'],
        setup: props => () =>
          vue.h('div', {
            class: 'mock-task-chat',
            'data-scope': props.scope,
            'data-execution-id': props.executionId ?? 'none',
            'data-draft-version': String(props.draftVersion ?? 0),
            'data-active-case-id': props.activeCaseId ?? 'none',
          }),
      })
      for (const component of sfc.matchAll(/import\s+(\w+)(?:,\s*\{[^}]*\})?\s+from\s+['"][^'"]+\.vue['"]/g)) {
        if (['UrlInput', 'TaskChat'].includes(component[1])) continue
        app.component(component[1], {
          setup: (_, { slots }) => () =>
            vue.h('div', Object.values(slots).flatMap(slot => slot({ item: {} }))),
        })
      }
      for (const icon of iconNames) app.component(icon, { render: () => null })
      return renderToString(app)
    },
  }
}

test('console history maps AI badge, round badge, title fallback, and running indicator accurately', async context => {
  const page = mountRequestConsole({
    listConsoleHistory: async () => ({
      items: [
        {
          id: 'ai-running-1',
          project_id: null,
          method: 'POST',
          url: 'http://127.0.0.1:8080/api/orders',
          request_headers: {},
          body_mode: 'json',
          body_preview: null,
          ok: false,
          status_code: null,
          elapsed_ms: 0,
          error_kind: null,
          error_message: null,
          response_headers: {},
          response_preview: null,
          response_bytes: 0,
          source: 'ai',
          title: '订单边界多轮回归',
          badge: 'AI 测试',
          status: 'running',
          total_rounds: 3,
          sub_case_count: 6,
          created_at: '2026-11-27T10:00:00Z',
        },
        {
          id: 'ai-done-2',
          project_id: null,
          method: 'GET',
          url: 'http://127.0.0.1:8080/health',
          request_headers: {},
          body_mode: 'none',
          body_preview: null,
          ok: true,
          status_code: 200,
          elapsed_ms: 420,
          error_kind: null,
          error_message: null,
          response_headers: {},
          response_preview: '{"ok":true}',
          response_bytes: 11,
          source: 'ai',
          title: null,
          badge: null,
          status: 'completed',
          total_rounds: 1,
          sub_case_count: 2,
          created_at: '2026-11-27T09:59:00Z',
        },
        {
          id: 'manual-1',
          project_id: null,
          method: 'GET',
          url: 'http://127.0.0.1:8010/health',
          request_headers: {},
          body_mode: 'none',
          body_preview: null,
          ok: true,
          status_code: 200,
          elapsed_ms: 15,
          error_kind: null,
          error_message: null,
          response_headers: {},
          response_preview: 'ok',
          response_bytes: 2,
          source: 'manual',
          title: null,
          badge: null,
          status: 'completed',
          total_rounds: 1,
          sub_case_count: 0,
          created_at: '2026-11-27T09:58:00Z',
        },
      ],
    }),
  })
  context.after(page.unmount)

  await page.state.loadHistory()
  const entries = page.state.historyEntries.value
  assert.equal(entries.length, 3)

  // AI running record with custom title & multi-round badge
  assert.equal(entries[0].label, '订单边界多轮回归')
  assert.equal(entries[0].running, true)
  assert.equal(entries[0].status, '运行中')
  assert.equal(entries[0].tone, 'info')
  assert.deepEqual(entries[0].badges, [
    { label: 'AI 测试', tone: 'info' },
    { label: '3 轮', tone: 'neutral' },
  ])

  // AI completed record without explicit title/badge falls back to method + url & default 'AI 测试'
  assert.equal(entries[1].label, 'GET http://127.0.0.1:8080/health')
  assert.equal(entries[1].running, false)
  assert.deepEqual(entries[1].badges, [{ label: 'AI 测试', tone: 'info' }])
  assert.equal(entries[1].tone, 'success')

  // Manual completed record has no AI badge, shows method + url and status code
  assert.equal(entries[2].label, 'GET http://127.0.0.1:8010/health')
  assert.equal(entries[2].running, false)
  assert.equal(entries[2].badges, undefined)
  assert.equal(entries[2].status, '200')
  assert.equal(entries[2].tone, 'success')

  // activeRunId marks a matching history entry as running even if list snapshot lagged
  page.state.activeRunId.value = 'manual-1'
  assert.equal(page.state.historyEntries.value[2].running, true)
  assert.equal(page.state.historyEntries.value[2].status, '运行中')
})

test('clicking a CaseLinkBlock loads sub-case snapshot and locks all request inputs; clicking 再次测试 unlocks and starts a new blank AI chat', async context => {
  let subCaseCalls = 0
  let sendCalls = 0
  const subCaseFixture = {
    id: 'sc-400',
    parent_id: 'hist-ai-1',
    round_index: 2,
    case_index: 1,
    label: '空用户名边界',
    method: 'POST',
    url: 'http://127.0.0.1:8080/api/user/login',
    request_headers: { 'Content-Type': 'application/json', Authorization: '[redacted]' },
    body_mode: 'json',
    request_body: '{"username":"","password":"123"}',
    ok: true,
    status_code: 400,
    elapsed_ms: 42,
    error_kind: null,
    error_message: null,
    response_headers: { 'content-type': ['application/json'] },
    response_preview: '{"error":"username required"}',
    response_bytes: 29,
    http_version: 'HTTP/1.1',
    response_reason: 'Bad Request',
    redirects: [],
    sse: null,
    assertion_summary: {
      passed: true,
      checks: [{ kind: 'status', expected: 400, actual: 400, passed: true }],
    },
    created_at: '2026-11-27T10:10:00Z',
  }

  const page = mountRequestConsole({
    getConsoleSubCase: async (historyId, caseId) => {
      subCaseCalls++
      assert.equal(historyId, 'hist-ai-1')
      assert.equal(caseId, 'sc-400')
      return subCaseFixture
    },
    readConsoleHistory: async () => ({
      id: 'hist-ai-1',
      project_id: null,
      method: 'POST',
      url: 'http://127.0.0.1:8080/api/user/login',
      request_headers: {},
      body_mode: 'json',
      body_preview: null,
      request_body: null,
      ok: true,
      status_code: 400,
      elapsed_ms: 120,
      error_kind: null,
      error_message: null,
      response_headers: {},
      response_preview: null,
      response_bytes: 0,
      source: 'ai',
      title: '登录边界测试',
      badge: 'AI 测试',
      status: 'completed',
      summary: '2 轮测试完成',
      total_rounds: 2,
      sub_case_count: 1,
      sub_cases: [subCaseFixture],
      created_at: '2026-11-27T10:10:00Z',
    }),
    sendConsoleRequest: async () => {
      sendCalls++
      return { ok: true, request: { method: 'POST', url: '' }, response: null, error_kind: null, error_message: null, elapsed_ms: 1 }
    },
  })
  context.after(page.unmount)

  const initialDraftVersion = page.state.draftVersion.value

  // Click CaseLinkBlock from AI assistant
  await page.state.handleSelectCase({
    type: 'case_link',
    scope: 'console',
    history_id: 'hist-ai-1',
    case_id: 'sc-400',
    round: 2,
    label: '空用户名边界',
    status: 'HTTP 400',
    tone: 'warning',
  })
  await settle()

  assert.equal(subCaseCalls, 1)
  assert.equal(page.state.isReadOnlyReplay.value, true)
  assert.equal(page.state.activeHistoryId.value, 'hist-ai-1')
  assert.equal(page.state.form.method, 'POST')
  assert.equal(page.state.form.url, 'http://127.0.0.1:8080/api/user/login')
  assert.equal(page.state.form.bodyMode, 'json')
  assert.equal(page.state.form.body, '{"username":"","password":"123"}')
  assert.equal(page.state.result.value.response.status, 400)
  assert.equal(page.state.result.value.response.body, '{"error":"username required"}')
  assert.deepEqual(page.workbenchRuns.readonlyReplay.console, {
    historyId: 'hist-ai-1',
    caseId: 'sc-400',
    round: 2,
    label: '空用户名边界',
  })

  // Locked mode prevents programmatic send, sample overwrite, prefix edits, and header edits
  const headerLenBefore = page.state.headerRows.value.length
  await page.state.send()
  page.state.selectUrlPrefix('https://')
  page.state.addHeaderRow()
  assert.equal(sendCalls, 0)
  assert.equal(page.state.urlPrefix.value, '')
  assert.equal(page.state.headerRows.value.length, headerLenBefore)

  // Rendered HTML reflects read-only banner, disabled inputs/controls, and TaskChat activeCaseId
  const lockedHtml = await page.render()
  assert.match(lockedHtml, /class="readonly-banner"/)
  assert.match(lockedHtml, /第 2 轮/)
  assert.match(lockedHtml, /空用户名边界/)
  assert.match(lockedHtml, /再次测试/)
  assert.match(lockedHtml, /data-disabled="true"/)
  assert.match(lockedHtml, /<select[^>]*class="method-select[^>]*disabled/)
  assert.match(lockedHtml, /<textarea[^>]*class="textarea mono body-editor"[^>]*disabled/)
  assert.match(lockedHtml, /data-active-case-id="sc-400"/)

  // Also verify headers tab renders disabled inputs/buttons while locked
  page.state.tab.value = 'headers'
  const lockedHeadersHtml = await page.render()
  assert.match(lockedHeadersHtml, /<input[^>]*disabled[^>]*readonly/)
  assert.match(lockedHeadersHtml, /class="btn btn-sm add-header"[^>]*disabled/)

  // Clicking 「再次测试」 unlocks inputs, clears activeHistoryId, clears result, increments draftVersion, and keeps request parameters editable
  page.state.retestCurrent()
  assert.equal(page.state.isReadOnlyReplay.value, false)
  assert.equal(page.state.readonlyReplay.value, null)
  assert.equal(page.workbenchRuns.readonlyReplay.console, null)
  assert.equal(page.state.activeHistoryId.value, null)
  assert.equal(page.workbenchRuns.activeHistoryId.console, null)
  assert.equal(page.state.draftVersion.value, initialDraftVersion + 1)
  assert.equal(page.state.result.value, null)
  assert.equal(page.state.form.method, 'POST')
  assert.equal(page.state.form.url, 'http://127.0.0.1:8080/api/user/login')
  assert.equal(page.state.form.body, '{"username":"","password":"123"}')

  // TaskChat receives executionId=none and incremented draftVersion -> brand new blank AI chat!
  const unlockedHtml = await page.render()
  assert.doesNotMatch(unlockedHtml, /class="readonly-banner"/)
  assert.match(unlockedHtml, /data-disabled="false"/)
  assert.match(unlockedHtml, /data-execution-id="none"/)
  assert.match(unlockedHtml, new RegExp(`data-draft-version="${initialDraftVersion + 1}"`))
  assert.match(unlockedHtml, /data-active-case-id="none"/)

  // Sending works again now that inputs are unlocked
  await page.state.send()
  assert.equal(sendCalls, 1)
})

test('@run-bound refreshes console history, sets activeHistoryId, exits stale read-only replay, and follows live progress', async context => {
  const liveGate = deferred()
  let emitFrame = null
  let listCalls = 0
  const followedIds = []

  const page = mountRequestConsole({
    listConsoleHistory: async () => {
      listCalls++
      return {
        items: [
          {
            id: 'ai-run-99',
            project_id: null,
            method: 'GET',
            url: 'http://127.0.0.1:8080/api/items',
            request_headers: {},
            body_mode: 'none',
            body_preview: null,
            ok: false,
            status_code: null,
            elapsed_ms: 0,
            error_kind: null,
            error_message: null,
            response_headers: {},
            response_preview: null,
            response_bytes: 0,
            source: 'ai',
            title: 'AI 列表接口巡检',
            badge: 'AI 测试',
            status: 'running',
            total_rounds: 2,
            sub_case_count: 0,
            created_at: '2026-11-27T10:20:00Z',
          },
        ],
      }
    },
    followConsoleRun: (runId, _signal, onFrame) => {
      followedIds.push(runId)
      emitFrame = onFrame
      return liveGate.promise
    },
  })
  context.after(page.unmount)

  // Pretend user was in a stale read-only replay before AI started a new suite
  page.state.readonlyReplay.value = { historyId: 'old-h', caseId: 'old-c', round: 1, label: '旧用例' }
  assert.equal(page.state.isReadOnlyReplay.value, true)

  await page.state.handleRunBound({
    scope: 'console',
    history_id: 'ai-run-99',
    previous_history_id: 'old-h',
    badge: 'AI 测试',
    title: 'AI 列表接口巡检',
    forked: true,
    run_id: 'asst-1',
  })
  await settle()

  assert.equal(page.state.isReadOnlyReplay.value, false)
  assert.equal(page.state.activeHistoryId.value, 'ai-run-99')
  assert.equal(page.state.activeRunId.value, 'ai-run-99')
  assert.equal(page.workbenchRuns.activeHistoryId.console, 'ai-run-99')
  assert.equal(page.workbenchRuns.activeRunId.console, 'ai-run-99')
  assert.equal(listCalls, 1)
  assert.deepEqual(followedIds, ['ai-run-99'])

  // Stream a cooling_down and case_done frame
  emitFrame({
    type: 'cooling_down',
    run_id: 'ai-run-99',
    round: 1,
    cooldown_ms: 500,
    cooldown_remaining_ms: 300,
  })
  assert.equal(page.state.activeRunProgress.value.stage, 'cooling_down')
  assert.match(await page.render(), /轮间冷却中 · 300ms/)

  liveGate.resolve({
    type: 'done',
    run_id: 'ai-run-99',
    status: 'completed',
    ok: true,
    summary: '共执行 2 条用例：全部通过',
    record: {
      id: 'ai-run-99',
      project_id: null,
      method: 'GET',
      url: 'http://127.0.0.1:8080/api/items',
      request_headers: {},
      body_mode: 'none',
      body_preview: null,
      request_body: null,
      ok: true,
      status_code: 200,
      elapsed_ms: 530,
      error_kind: null,
      error_message: null,
      response_headers: { 'content-type': ['application/json'] },
      response_preview: '[]',
      response_bytes: 2,
      source: 'ai',
      title: 'AI 列表接口巡检',
      badge: 'AI 测试',
      status: 'completed',
      summary: '共执行 2 条用例：全部通过',
      total_rounds: 2,
      sub_case_count: 1,
      sub_cases: [
        {
          id: 'sc-1',
          parent_id: 'ai-run-99',
          round_index: 1,
          case_index: 0,
          label: '默认列表',
          method: 'GET',
          url: 'http://127.0.0.1:8080/api/items',
          request_headers: {},
          body_mode: 'none',
          request_body: null,
          ok: true,
          status_code: 200,
          elapsed_ms: 15,
          error_kind: null,
          error_message: null,
          response_headers: {},
          response_preview: '[]',
          response_bytes: 2,
          http_version: 'HTTP/1.1',
          response_reason: 'OK',
          redirects: [],
          sse: null,
          assertion_summary: null,
          created_at: '2026-11-27T10:20:01Z',
        },
      ],
      created_at: '2026-11-27T10:20:00Z',
    },
  })
  await settle()

  assert.equal(page.state.activeRunId.value, null)
  assert.equal(page.workbenchRuns.activeRunId.console, null)
  assert.equal(page.state.sending.value, false)
  assert.equal(listCalls, 2)
  assert.equal(page.state.suiteCases.value.length, 1)
})

test('onBeforeUnmount NEVER cancels backend run; remount restores form draft and auto-reconnects active run via getActiveConsoleRuns + followConsoleRun', async () => {
  const sharedPinia = pinia.createPinia()
  const firstFollow = deferred()
  const secondFollow = deferred()
  let firstSignal = null
  let secondOnFrame = null
  let cancelCalls = 0
  const followedRunIds = []

  // First mount: user edits form and starts a persistent console run
  const first = mountRequestConsole({
    pinia: sharedPinia,
    startConsoleRun: async payload => ({
      id: 'bg-console-run-1',
      source: 'manual',
      title: null,
      badge: null,
      status: 'running',
      stage: 'executing',
      method: payload.method,
      url: payload.url,
      ok: false,
      status_code: null,
      elapsed_ms: 0,
      error_kind: null,
      error_message: null,
      total_rounds: 1,
      current_round: 1,
      total_cases: 1,
      completed_cases: 0,
      cooldown_ms: 500,
      cooldown_remaining_ms: null,
      summary: null,
      created_at: '2026-11-27T10:30:00Z',
    }),
    followConsoleRun: (runId, signal) => {
      followedRunIds.push(runId)
      firstSignal = signal
      return firstFollow.promise
    },
    cancelConsoleRun: async () => {
      cancelCalls++
      return null
    },
    getActiveConsoleRuns: async () => ({ items: [] }),
    listConsoleHistory: async () => ({ items: [] }),
  })

  first.state.form.method = 'POST'
  first.state.form.url = 'http://127.0.0.1:8080/api/sse-stream'
  first.state.form.bodyMode = 'json'
  first.state.form.body = '{"stream":true}'
  first.state.headerRows.value = [{ name: 'X-Trace', value: 'trace-77' }]

  const sendPromise = first.state.send()
  await settle()
  assert.equal(first.state.activeRunId.value, 'bg-console-run-1')
  assert.equal(first.workbenchRuns.activeRunId.console, 'bg-console-run-1')

  // Simulate navigating away to another page: unmount aborts local reader ONLY and preserves backend run & draft state
  first.unmount()
  firstFollow.reject(new Error('local observer aborted'))
  await sendPromise

  assert.equal(firstSignal.aborted, true)
  assert.equal(cancelCalls, 0)
  assert.equal(first.workbenchRuns.activeRunId.console, 'bg-console-run-1')
  assert.equal(first.consoleStore.form.url, 'http://127.0.0.1:8080/api/sse-stream')
  assert.equal(first.consoleStore.form.body, '{"stream":true}')

  // Second mount (navigating back): restores form draft and reconnects to bg-console-run-1
  const second = mountRequestConsole({
    pinia: sharedPinia,
    getActiveConsoleRuns: async () => ({
      items: [
        {
          id: 'bg-console-run-1',
          source: 'manual',
          title: null,
          badge: null,
          status: 'running',
          stage: 'executing',
          method: 'POST',
          url: 'http://127.0.0.1:8080/api/sse-stream',
          ok: false,
          status_code: null,
          elapsed_ms: 150,
          error_kind: null,
          error_message: null,
          total_rounds: 1,
          current_round: 1,
          total_cases: 1,
          completed_cases: 0,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          summary: null,
          created_at: '2026-11-27T10:30:00Z',
        },
      ],
    }),
    followConsoleRun: (runId, _signal, onFrame) => {
      followedRunIds.push(runId)
      secondOnFrame = onFrame
      return secondFollow.promise
    },
    cancelConsoleRun: async runId => {
      cancelCalls++
      return { id: runId, status: 'cancelled', ok: false, method: 'POST', url: 'http://127.0.0.1:8080/api/sse-stream', request_headers: {}, body_mode: 'json', body_preview: null, elapsed_ms: 200, error_kind: 'cancelled', error_message: 'cancelled', response_headers: {}, response_preview: null, response_bytes: 0, created_at: '2026-11-27T10:30:00Z' }
    },
    listConsoleHistory: async () => ({ items: [] }),
  })

  // Form draft restored from consoleWorkbench store immediately
  assert.equal(second.state.form.method, 'POST')
  assert.equal(second.state.form.url, 'http://127.0.0.1:8080/api/sse-stream')
  assert.equal(second.state.form.body, '{"stream":true}')
  assert.equal(second.state.headerRows.value[0].name, 'X-Trace')

  second.triggerMounted()
  await settle()

  assert.deepEqual(followedRunIds, ['bg-console-run-1', 'bg-console-run-1'])
  assert.equal(second.state.sending.value, true)
  assert.equal(second.state.activeRunId.value, 'bg-console-run-1')

  // Receive live SSE event and final result after reconnect
  secondOnFrame({ type: 'sse_event', event: { event: 'delta', data: 'chunk-1' } })
  assert.equal(second.state.liveEvents.value.length, 1)
  assert.equal(second.state.liveEvents.value[0].data, 'chunk-1')

  secondFollow.resolve({
    type: 'result',
    result: {
      ok: true,
      history_id: 'bg-console-run-1',
      request: { method: 'POST', url: 'http://127.0.0.1:8080/api/sse-stream' },
      response: {
        status: 200,
        reason: 'OK',
        http_version: 'HTTP/1.1',
        headers: { 'content-type': ['text/event-stream'] },
        body: 'data: chunk-1\n\n',
        body_bytes: 15,
        body_truncated: false,
        body_encoding: null,
        elapsed_ms: 480,
        redirects: [],
        sse: { events: [{ event: 'delta', data: 'chunk-1' }], stop_reason: 'observation_complete' },
      },
      error_kind: null,
      error_message: null,
      elapsed_ms: 480,
    },
  })
  await settle()

  assert.equal(second.state.sending.value, false)
  assert.equal(second.state.activeRunId.value, null)
  assert.equal(second.workbenchRuns.activeRunId.console, null)
  assert.equal(second.state.result.value.response.status, 200)
  assert.equal(second.state.result.value.response.sse.events[0].data, 'chunk-1')
  assert.equal(cancelCalls, 0)

  // Also verify explicit stopActiveRun calls cancelConsoleRun
  second.state.activeRunId.value = 'bg-stop-2'
  await second.state.stopActiveRun()
  assert.equal(cancelCalls, 1)
  assert.equal(second.state.activeRunId.value, null)
  second.unmount()
})

test('restoring an AI multi-round history shows sub-cases overview and selecting a sub-case enters read-only replay', async context => {
  const aiDetail = {
    id: 'ai-multi-1',
    project_id: null,
    method: 'POST',
    url: 'http://127.0.0.1:8080/api/echo',
    request_headers: { 'Content-Type': 'application/json' },
    body_mode: 'json',
    body_preview: '{"msg":"hi"}',
    request_body: '{"msg":"hi"}',
    ok: true,
    status_code: 200,
    elapsed_ms: 610,
    error_kind: null,
    error_message: null,
    response_headers: { 'content-type': ['application/json'] },
    response_preview: '{"echo":"hi"}',
    response_bytes: 13,
    source: 'ai',
    title: 'Echo 多轮压测',
    badge: 'AI 测试',
    status: 'completed',
    summary: '共执行 2 条用例（2 轮，冷却 500 ms）：通过 2 条。',
    total_rounds: 2,
    cooldown_ms: 500,
    sub_case_count: 2,
    sub_cases: [
      {
        id: 'sc-r1',
        parent_id: 'ai-multi-1',
        round_index: 1,
        case_index: 0,
        label: '首轮正常请求',
        method: 'POST',
        url: 'http://127.0.0.1:8080/api/echo',
        request_headers: { 'Content-Type': 'application/json' },
        body_mode: 'json',
        request_body: '{"msg":"round1"}',
        ok: true,
        status_code: 200,
        elapsed_ms: 21,
        error_kind: null,
        error_message: null,
        response_headers: { 'content-type': ['application/json'] },
        response_preview: '{"echo":"round1"}',
        response_bytes: 17,
        http_version: 'HTTP/1.1',
        response_reason: 'OK',
        redirects: [],
        sse: null,
        assertion_summary: null,
        created_at: '2026-11-27T11:00:00Z',
      },
      {
        id: 'sc-r2',
        parent_id: 'ai-multi-1',
        round_index: 2,
        case_index: 0,
        label: '二轮超长边界',
        method: 'POST',
        url: 'http://127.0.0.1:8080/api/echo',
        request_headers: { 'Content-Type': 'application/json' },
        body_mode: 'json',
        request_body: '{"msg":"round2"}',
        ok: true,
        status_code: 413,
        elapsed_ms: 18,
        error_kind: null,
        error_message: null,
        response_headers: {},
        response_preview: '{"error":"too large"}',
        response_bytes: 21,
        http_version: 'HTTP/1.1',
        response_reason: 'Payload Too Large',
        redirects: [],
        sse: null,
        assertion_summary: null,
        created_at: '2026-11-27T11:00:01Z',
      },
    ],
    created_at: '2026-11-27T11:00:00Z',
  }

  const page = mountRequestConsole({
    readConsoleHistory: async () => aiDetail,
  })
  context.after(page.unmount)

  await page.state.restoreById('ai-multi-1')
  assert.equal(page.state.isReadOnlyReplay.value, false)
  assert.equal(page.state.suiteCases.value.length, 2)

  const overviewHtml = await page.render()
  assert.match(overviewHtml, /class="suite-overview"/)
  assert.match(overviewHtml, /Echo 多轮压测/)
  assert.match(overviewHtml, /首轮正常请求/)
  assert.match(overviewHtml, /二轮超长边界/)
  assert.match(overviewHtml, /class="btn btn-sm retest-btn"/)

  page.state.selectSubCase(aiDetail.sub_cases[1])
  assert.equal(page.state.isReadOnlyReplay.value, true)
  assert.equal(page.state.form.body, '{"msg":"round2"}')
  assert.equal(page.state.result.value.response.status, 413)
  assert.equal(page.workbenchRuns.readonlyReplay.console.caseId, 'sc-r2')
})

test('RequestConsole forwards console settings including cooldownMs to buildConsoleTestContext and TaskChat, and guards unmount against backend cancel', () => {
  const page = mountRequestConsole()
  page.consoleSettings.cooldownMs = 1200
  page.consoleSettings.timeoutMs = 15000

  assert.equal(page.state.consoleSettingsPayload.value.cooldownMs, 1200)
  assert.equal(page.state.consoleSettingsPayload.value.timeoutMs, 15000)
  assert.equal(page.state.assistantContext.value.settings.cooldown_ms, 1200)
  assert.equal(page.state.assistantContext.value.settings.timeout_ms, 15000)
  page.unmount()

  const source = fs.readFileSync(path.join(__dirname, '../src/views/RequestConsole.vue'), 'utf8')
  const unmountBlock = source.slice(source.indexOf('onBeforeUnmount('), source.indexOf('function clearResponse'))
  assert.match(unmountBlock, /observerAbort\.abort\(\)/)
  assert.doesNotMatch(unmountBlock, /cancelConsoleRun/)
})
