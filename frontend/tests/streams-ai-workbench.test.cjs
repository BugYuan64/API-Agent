const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')
const pinia = require('pinia')
const { compile } = require('@vue/compiler-dom')
const { renderToString } = require('@vue/server-renderer')

globalThis.fetch = () => {
  throw new Error('Network is prohibited in frontend tests')
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

const settle = async (ticks = 10) => {
  for (let i = 0; i < ticks; i++) await Promise.resolve()
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    get length() {
      return values.size
    },
    key: (index) => [...values.keys()][index] ?? null,
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
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

const formatExports = loadTsModule('src/lib/format.ts')
const resultExports = loadTsModule('src/lib/results.ts')
const prefixExports = loadTsModule('src/lib/urlPrefixes.ts')
const assistantContextExports = loadTsModule('src/lib/assistantContext.ts')
const headerRowsExports = loadTsModule('src/composables/useHeaderRows.ts', {
  require: (id) => {
    if (id === 'vue') return vue
    throw new Error(`Unexpected require: ${id}`)
  },
})
const workbenchRunsMod = loadTsModule('src/stores/workbenchRuns.ts', {
  require: (id) => {
    if (id === 'pinia') return pinia
    if (id === 'vue') return vue
    throw new Error(`Unexpected require: ${id}`)
  },
})
const streamWorkbenchMod = loadTsModule('src/stores/streamWorkbench.ts', {
  require: (id) => {
    if (id === 'pinia') return pinia
    if (id === 'vue') return vue
    throw new Error(`Unexpected require: ${id}`)
  },
})

function mountLongConnection(mocks = {}) {
  const sfc = fs.readFileSync(path.join(__dirname, '../src/views/LongConnection.vue'), 'utf8')
  const script = sfc
    .split('<script setup lang="ts">')[1]
    .split('</script>')[0]
    .replace(/^\s*import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/gm, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText

  const storage = mocks.storage ?? memoryStorage()
  const timers = new Map()
  let timerId = 0
  const mockWindow = {
    localStorage: storage,
    addEventListener: () => {},
    removeEventListener: () => {},
    setTimeout: (fn) => {
      timers.set(++timerId, fn)
      return timerId
    },
    clearTimeout: (id) => timers.delete(id),
    setInterval: (fn) => {
      timers.set(++timerId, fn)
      return timerId
    },
    clearInterval: (id) => timers.delete(id),
  }

  const chatHistoryExports = loadTsModule('src/lib/chatHistory.ts', { window: mockWindow })
  const testSettingsExports = loadTsModule('src/lib/testSettings.ts', {
    require: (id) => {
      if (id === 'vue') return vue
      throw new Error(`Unexpected require: ${id}`)
    },
    window: mockWindow,
  })

  const mounted = []
  const unmounted = []
  const toasts = []
  const pollingExports = loadTsModule('src/composables/usePolling.ts', {
    require: (id) => {
      if (id === 'vue') return { ...vue, onBeforeUnmount: (fn) => unmounted.push(fn) }
      throw new Error(`Unexpected require: ${id}`)
    },
    window: mockWindow,
  })

  const appPinia = mocks.pinia ?? pinia.createPinia()
  pinia.setActivePinia(appPinia)

  const scope = vue.effectScope()
  const { pinia: _ignorePinia, storage: _ignoreStorage, ...overrideMocks } = mocks

  const env = {
    ...vue,
    describeStopReason: formatExports.describeStopReason,
    formatBytes: formatExports.formatBytes,
    formatDuration: formatExports.formatDuration,
    formatRelative: formatExports.formatRelative,
    looksLikeJson: formatExports.looksLikeJson,
    prettyJson: formatExports.prettyJson,
    ...resultExports,
    ...prefixExports,
    ...assistantContextExports,
    ...chatHistoryExports,
    ...testSettingsExports,
    useHeaderRows: headerRowsExports.useHeaderRows,
    usePolling: pollingExports.usePolling,
    useWorkbenchRunsStore: workbenchRunsMod.useWorkbenchRunsStore,
    useStreamWorkbenchStore: streamWorkbenchMod.useStreamWorkbenchStore,
    useToastStore: () => ({ push: (...args) => toasts.push(args) }),
    describeError: (error) => String(error),
    onMounted: (fn) => mounted.push(fn),
    onBeforeUnmount: (fn) => unmounted.push(fn),
    listStreams: async () => ({ items: [] }),
    getActiveStreams: async () => ({ items: [], manual_sessions: [], ai_suites: [] }),
    readStreamInputs: async () => ({
      url: 'ws://127.0.0.1:8010/ws',
      request_headers: {},
      subprotocols: [],
      scheduled_messages: [],
    }),
    readStream: async (id) => ({
      session: { id, kind: 'websocket', url: 'ws://127.0.0.1:8010/ws', status: 'closed', finished: true, event_count: 0, sent_count: 0, byte_count: 0, truncated_count: 0 },
      events: [],
      next_cursor: 0,
      finished: true,
      has_more: false,
    }),
    listStreamSubCases: async (id) => ({ session_id: id, items: [], total: 0 }),
    getStreamSubCase: async () => {
      throw new Error('Unexpected getStreamSubCase')
    },
    followStreamSuite: async () => {
      throw new Error('Unexpected followStreamSuite')
    },
    cancelStreamSuite: async () => {
      throw new Error('Unexpected cancelStreamSuite')
    },
    startStream: async () => {
      throw new Error('Unexpected startStream')
    },
    stopStream: async () => {
      throw new Error('Unexpected stopStream')
    },
    sendStreamMessage: async () => {
      throw new Error('Unexpected sendStreamMessage')
    },
    deleteStream: async () => null,
    window: mockWindow,
    ...overrideMocks,
  }

  const bindings = []
  function collect(node) {
    if (ts.isIdentifier(node)) bindings.push(node.text)
    else node.elements.forEach((entry) => {
      if (!ts.isOmittedExpression(entry)) collect(entry.name)
    })
  }
  for (const stmt of ts.createSourceFile('LongConnection.vue', script, ts.ScriptTarget.Latest, true).statements) {
    if (ts.isVariableStatement(stmt)) stmt.declarationList.declarations.forEach((entry) => collect(entry.name))
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
  const iconNames = icons.split(',').map((v) => v.trim()).filter(Boolean)

  return {
    state,
    toasts,
    webSocketSettings: testSettingsExports.webSocketSettings,
    pinia: appPinia,
    workbenchRuns: workbenchRunsMod.useWorkbenchRunsStore(),
    streamWorkbench: streamWorkbenchMod.useStreamWorkbenchStore(),
    triggerMounted: () => mounted.forEach((fn) => fn()),
    tick: () => {
      const [id, fn] = [...timers][0] ?? []
      assert.equal(typeof fn, 'function', 'expected a scheduled timer')
      timers.delete(id)
      return fn()
    },
    timerCount: () => timers.size,
    unmount: () => {
      unmounted.forEach((fn) => fn())
      scope.stop()
    },
    render: async () => {
      const capturedTaskChatProps = []
      const capturedUrlInputProps = []
      const ctx = vue.proxyRefs({
        ...env,
        $slots: {},
        ...Object.fromEntries(iconNames.map((icon) => [icon, {}])),
        ...state,
      })
      const app = vue.createSSRApp({ render: () => render(ctx, []) })
      for (const component of sfc.matchAll(/import\s+(\w+)(?:,\s*\{[^}]*\})?\s+from\s+['"][^'"]+\.vue['"]/g)) {
        const compName = component[1]
        if (compName === 'TaskChat') {
          app.component('TaskChat', {
            props: ['scope', 'executionId', 'draftVersion', 'currentTest', 'settings', 'activeCaseId'],
            setup: (props) => () => {
              capturedTaskChatProps.push({ ...props })
              return vue.h('div', { class: 'mock-task-chat', 'data-active-case': props.activeCaseId ?? '' })
            },
          })
        } else if (compName === 'UrlInput') {
          app.component('UrlInput', {
            props: ['modelValue', 'address', 'label', 'placeholder', 'websocket', 'disabled'],
            setup: (props) => () => {
              capturedUrlInputProps.push({ ...props })
              return vue.h('input', {
                class: 'mock-url-input',
                value: props.address,
                disabled: Boolean(props.disabled),
              })
            },
          })
        } else {
          app.component(compName, {
            setup: (_, { slots }) => () =>
              vue.h('div', Object.values(slots).flatMap((slot) => slot({ item: {} }))),
          })
        }
      }
      for (const icon of iconNames) app.component(icon, { render: () => null })
      const html = await renderToString(app)
      return { html, taskChatProps: capturedTaskChatProps[0], urlInputProps: capturedUrlInputProps[0] }
    },
  }
}

test('LongConnection maps AI badges, multi-round badges, running indicators, and loads sub-cases on AI suite replay', async (context) => {
  const subCaseCalls = []
  const page = mountLongConnection({
    listStreams: async () => ({
      items: [
        {
          id: 'ai-suite-1',
          kind: 'websocket',
          url: 'ws://127.0.0.1:8088/api/ws/chat',
          request_headers: {},
          status: 'running',
          stop_reason: null,
          event_count: 14,
          byte_count: 420,
          sent_count: 4,
          truncated_count: 0,
          settings: {},
          opened_at: '2026-11-27T10:00:00Z',
          closed_at: null,
          created_at: '2026-11-27T10:00:00Z',
          source: 'ai',
          title: '聊天室多轮稳定性测试',
          badge: 'AI 测试',
          total_rounds: 3,
          sub_case_count: 6,
          summary: '共计划 3 轮 6 条 WebSocket 用例',
        },
        {
          id: 'ai-suite-fallback-badge',
          kind: 'websocket',
          url: 'ws://127.0.0.1:8088/api/ws/echo',
          request_headers: {},
          status: 'completed',
          stop_reason: null,
          event_count: 8,
          byte_count: 200,
          sent_count: 2,
          truncated_count: 0,
          settings: {},
          opened_at: '2026-11-27T09:50:00Z',
          closed_at: '2026-11-27T09:51:00Z',
          created_at: '2026-11-27T09:50:00Z',
          source: 'ai',
          title: null,
          badge: null,
          total_rounds: 1,
          sub_case_count: 2,
          summary: '2 条用例全部通过',
        },
        {
          id: 'manual-live-1',
          kind: 'websocket',
          url: 'ws://127.0.0.1:9000/live',
          request_headers: {},
          status: 'open',
          stop_reason: null,
          event_count: 3,
          byte_count: 90,
          sent_count: 1,
          truncated_count: 0,
          settings: {},
          opened_at: '2026-11-27T09:40:00Z',
          closed_at: null,
          created_at: '2026-11-27T09:40:00Z',
          source: 'manual',
        },
        {
          id: 'manual-closed-1',
          kind: 'websocket',
          url: 'ws://127.0.0.1:9000/done',
          request_headers: {},
          status: 'closed',
          stop_reason: 'client_stopped',
          event_count: 5,
          byte_count: 120,
          sent_count: 2,
          truncated_count: 0,
          settings: {},
          opened_at: '2026-11-27T09:30:00Z',
          closed_at: '2026-11-27T09:31:00Z',
          created_at: '2026-11-27T09:30:00Z',
          source: 'manual',
        },
      ],
    }),
    readStreamInputs: async () => ({
      url: 'ws://127.0.0.1:8088/api/ws/echo',
      request_headers: { 'X-Suite': 'ai' },
      subprotocols: ['chat.v1'],
      scheduled_messages: ['ping'],
    }),
    readStream: async (id) => ({
      session: {
        id,
        kind: 'websocket',
        url: 'ws://127.0.0.1:8088/api/ws/echo',
        status: 'completed',
        finished: true,
        stop_reason: null,
        error_kind: null,
        error_message: null,
        http_status: 101,
        content_type: null,
        subprotocol: null,
        close_code: null,
        close_reason: null,
        event_count: 8,
        byte_count: 200,
        sent_count: 2,
        truncated_count: 0,
        opened_at: '2026-11-27T09:50:00Z',
        closed_at: '2026-11-27T09:51:00Z',
        source: 'ai',
        title: '回显测试套件',
        badge: 'AI 测试',
        summary: '2 条用例全部通过',
        sub_case_count: 2,
      },
      events: [],
      next_cursor: 0,
      finished: true,
      has_more: false,
    }),
    listStreamSubCases: async (id) => {
      subCaseCalls.push(id)
      return {
        session_id: id,
        total: 2,
        items: [
          {
            id: 'sub-1',
            parent_session_id: id,
            round_index: 1,
            case_index: 0,
            case_label: '正常握手回显',
            label: '正常握手回显',
            url: 'ws://127.0.0.1:8088/api/ws/echo',
            status: 'closed',
            stop_reason: 'target_closed',
            error_kind: null,
            error_message: null,
            http_status: 101,
            subprotocol: null,
            close_code: 1000,
            close_reason: '',
            event_count: 4,
            byte_count: 100,
            sent_count: 1,
            truncated_count: 0,
            opened_at: null,
            closed_at: null,
            created_at: '2026-11-27T09:50:00Z',
          },
          {
            id: 'sub-2',
            parent_session_id: id,
            round_index: 1,
            case_index: 1,
            case_label: '异常鉴权拒绝',
            label: '异常鉴权拒绝',
            url: 'ws://127.0.0.1:8088/api/ws/echo',
            status: 'error',
            stop_reason: 'handshake_rejected',
            error_kind: 'handshake_rejected',
            error_message: 'HTTP 401',
            http_status: 401,
            subprotocol: null,
            close_code: null,
            close_reason: null,
            event_count: 1,
            byte_count: 32,
            sent_count: 0,
            truncated_count: 0,
            opened_at: null,
            closed_at: null,
            created_at: '2026-11-27T09:50:05Z',
          },
        ],
      }
    },
  })
  context.after(page.unmount)

  await page.state.loadHistory()
  const entries = page.state.historyEntries.value
  assert.equal(entries.length, 4)

  // AI multi-round running suite
  assert.equal(entries[0].label, '聊天室多轮稳定性测试')
  assert.equal(entries[0].running, true)
  assert.equal(entries[0].tone, 'info')
  assert.deepEqual(entries[0].badges[0], { label: 'AI 测试', tone: 'info' })
  assert.equal(entries[0].badges[1].label, '3 轮')
  assert.match(entries[0].meta, /6 用例 · 14 条/)

  // AI suite with null title/badge falls back cleanly
  assert.equal(entries[1].label, 'WS · ws://127.0.0.1:8088/api/ws/echo')
  assert.equal(entries[1].running, false)
  assert.equal(entries[1].tone, 'success')
  assert.deepEqual(entries[1].badges, [{ label: 'AI 测试', tone: 'info' }])

  // Manual open session marked running
  assert.equal(entries[2].running, true)
  assert.equal(entries[2].badges, undefined)
  assert.equal(entries[2].status, '已连接')

  // Manual closed session not running
  assert.equal(entries[3].running, false)
  assert.equal(entries[3].badges, undefined)
  assert.equal(entries[3].tone, 'success')
  assert.equal(entries[3].status, '手动停止')

  // Replaying grouped AI suite loads sub-cases and displays suite summary & chips
  await page.state.replay(page.state.history.value[1])
  assert.deepEqual(subCaseCalls, ['ai-suite-fallback-badge'])
  assert.equal(page.state.subCases.value.length, 2)

  const { html } = await page.render()
  assert.match(html, /class="suite-panel"/)
  assert.match(html, /2 条用例全部通过/)
  assert.match(html, /class="subcase-chip[^"]*"[\s\S]*正常握手回显/)
  assert.match(html, /class="subcase-chip[^"]*"[\s\S]*异常鉴权拒绝/)
})

test('select-case loads sub-case snapshot, locks all inputs in read-only replay, and 再次测试 unlocks with new blank AI chat', async (context) => {
  const startedStreams = []
  const sentMessages = []
  const subCaseRequests = []
  const page = mountLongConnection({
    getStreamSubCase: async (historyId, caseId) => {
      subCaseRequests.push({ historyId, caseId })
      return {
        session_id: historyId,
        case_id: caseId,
        round_index: 2,
        case_index: 1,
        case_label: '第 2 轮 · 心跳重连验证',
        label: '第 2 轮 · 心跳重连验证',
        readonly: true,
        inputs: {
          url: 'ws://127.0.0.1:8088/api/ws/chat?room=vip',
          request_headers: { Authorization: '[redacted]', 'X-Client': 'suite-r2' },
          subprotocols: ['chat.v2'],
          scheduled_messages: ['{"type":"ping"}', '{"type":"echo","msg":"hi"}'],
        },
        session: {
          id: caseId,
          kind: 'websocket',
          url: 'ws://127.0.0.1:8088/api/ws/chat?room=vip',
          status: 'closed',
          finished: true,
          stop_reason: 'target_closed',
          error_kind: null,
          error_message: null,
          http_status: 101,
          content_type: null,
          subprotocol: 'chat.v2',
          close_code: 1000,
          close_reason: 'normal',
          event_count: 2,
          byte_count: 48,
          sent_count: 1,
          truncated_count: 0,
          opened_at: '2026-11-27T10:05:00Z',
          closed_at: '2026-11-27T10:05:02Z',
          source: 'ai',
          parent_session_id: historyId,
          round_index: 2,
          case_index: 1,
          case_label: '第 2 轮 · 心跳重连验证',
        },
        events: [
          {
            session_id: caseId,
            sequence: 1,
            direction: 'out',
            kind: 'text',
            event_name: null,
            data: '{"type":"ping"}',
            data_bytes: 15,
            truncated: false,
            elapsed_ms: 10,
          },
          {
            session_id: caseId,
            sequence: 2,
            direction: 'in',
            kind: 'text',
            event_name: null,
            data: '{"type":"pong","ok":true}',
            data_bytes: 25,
            truncated: false,
            elapsed_ms: 28,
          },
        ],
      }
    },
    startStream: async (payload) => {
      startedStreams.push(payload)
      return {
        id: 'ws-fresh-after-retest',
        kind: 'websocket',
        url: payload.url,
        status: 'closed',
        finished: true,
        stop_reason: 'target_closed',
        error_kind: null,
        error_message: null,
        http_status: 101,
        content_type: null,
        subprotocol: null,
        close_code: 1000,
        close_reason: null,
        event_count: 0,
        byte_count: 0,
        sent_count: 0,
        truncated_count: 0,
        opened_at: null,
        closed_at: null,
      }
    },
    sendStreamMessage: async (id, msg) => {
      sentMessages.push({ id, msg })
      return page.state.session.value
    },
  })
  context.after(page.unmount)

  const initialDraftVersion = page.state.draftVersion.value

  // Click a CaseLinkBlock emitted by TaskChat
  await page.state.handleSelectCase({
    type: 'case_link',
    scope: 'websocket',
    history_id: 'suite-hist-42',
    case_id: 'sub-case-r2-1',
    round: 2,
    label: '第 2 轮 · 心跳重连验证',
    status: 'passed',
    tone: 'success',
  })

  assert.deepEqual(subCaseRequests, [{ historyId: 'suite-hist-42', caseId: 'sub-case-r2-1' }])
  assert.equal(page.state.isReadOnlyReplay.value, true)
  assert.equal(page.state.activeHistoryId.value, 'suite-hist-42')
  assert.equal(page.state.form.url, 'ws://127.0.0.1:8088/api/ws/chat?room=vip')
  assert.equal(page.state.form.subprotocols, 'chat.v2')
  assert.equal(page.state.form.scheduled, '{"type":"ping"}\n{"type":"echo","msg":"hi"}')
  assert.equal(page.state.headerRows.value.length, 2)
  assert.equal(page.state.events.value.length, 2)
  assert.equal(page.state.events.value[1].data, '{"type":"pong","ok":true}')
  assert.deepEqual(page.workbenchRuns.readonlyReplay.websocket, {
    historyId: 'suite-hist-42',
    caseId: 'sub-case-r2-1',
    round: 2,
    label: '第 2 轮 · 心跳重连验证',
  })

  // Guarded mutations & execution are blocked while in read-only replay lock
  page.state.selectUrlPrefix('wss://blocked.example')
  page.state.applySample(page.state.SAMPLES[0])
  page.state.messageDraft.value = 'should-not-edit'
  await page.state.start()
  await page.state.sendMessage()
  assert.equal(page.state.form.url, 'ws://127.0.0.1:8088/api/ws/chat?room=vip')
  assert.equal(page.state.form.scheduled, '{"type":"ping"}\n{"type":"echo","msg":"hi"}')
  assert.deepEqual(startedStreams, [])
  assert.deepEqual(sentMessages, [])

  // Render checks: read-only banner, locked inputs, events rendered, and TaskChat props forwarded
  const lockedRender = await page.render()
  assert.equal(lockedRender.urlInputProps.disabled, true)
  assert.equal(lockedRender.taskChatProps.scope, 'websocket')
  assert.equal(lockedRender.taskChatProps.executionId, 'suite-hist-42')
  assert.equal(lockedRender.taskChatProps.activeCaseId, 'sub-case-r2-1')
  assert.equal(lockedRender.taskChatProps.settings.cooldownMs, page.webSocketSettings.cooldownMs)
  assert.equal(lockedRender.taskChatProps.currentTest.settings.cooldown_ms, page.webSocketSettings.cooldownMs)
  assert.match(lockedRender.html, /class="readonly-banner"/)
  assert.match(lockedRender.html, /只读回放/)
  assert.match(lockedRender.html, /第 2 轮 · 心跳重连验证/)
  assert.match(lockedRender.html, /再次测试/)
  assert.match(lockedRender.html, /<textarea[^>]*disabled/)
  assert.match(lockedRender.html, /placeholder="名称"[^>]*disabled/)
  assert.match(lockedRender.html, /placeholder="值"[^>]*disabled/)
  assert.match(lockedRender.html, /placeholder="graphql-ws"[^>]*disabled/)
  assert.match(lockedRender.html, /class="sample-chip"[^>]*disabled/)
  assert.match(lockedRender.html, /class="btn btn-sm add-header-btn"[^>]*disabled/)
  assert.match(lockedRender.html, /class="btn btn-accent start-button"[^>]*disabled/)
  assert.match(lockedRender.html, /pong/)

  // Click 「再次测试」: unlocks inputs, clears activeHistoryId/session/events, increments draftVersion for fresh blank AI chat
  page.state.retest()
  assert.equal(page.state.isReadOnlyReplay.value, false)
  assert.equal(page.workbenchRuns.readonlyReplay.websocket, null)
  assert.equal(page.state.activeHistoryId.value, null)
  assert.equal(page.state.session.value, null)
  assert.deepEqual(page.state.events.value, [])
  assert.equal(page.state.draftVersion.value, initialDraftVersion + 1)
  // Form inputs remain populated and unlocked for editing/reconnecting
  assert.equal(page.state.form.url, 'ws://127.0.0.1:8088/api/ws/chat?room=vip')

  const unlockedRender = await page.render()
  assert.equal(unlockedRender.urlInputProps.disabled, false)
  assert.equal(unlockedRender.taskChatProps.executionId, null)
  assert.equal(unlockedRender.taskChatProps.activeCaseId, null)
  assert.equal(unlockedRender.taskChatProps.draftVersion, initialDraftVersion + 1)
  assert.doesNotMatch(unlockedRender.html, /class="readonly-banner"/)

  // User can immediately start a brand-new connection after 再次测试
  await page.state.start()
  assert.equal(startedStreams.length, 1)
  assert.equal(startedStreams[0].url, 'ws://127.0.0.1:8088/api/ws/chat?room=vip')
  assert.equal(page.state.activeHistoryId.value, 'ws-fresh-after-retest')

  // newTest clears everything including form fields and increments draftVersion again
  page.state.newTest()
  assert.equal(page.state.form.url, '')
  assert.equal(page.state.form.scheduled, '')
  assert.equal(page.state.activeHistoryId.value, null)
  assert.equal(page.state.session.value, null)
  assert.equal(page.state.draftVersion.value, initialDraftVersion + 2)
})

test('@run-bound refreshes history, clears stale readonly replay, follows AI suite live frames, and supports explicit cancel', async (context) => {
  let historyCalls = 0
  let onSuiteFrame = null
  const suiteGate = deferred()
  const followedIds = []
  const cancelledIds = []
  const subCaseFetches = []

  const page = mountLongConnection({
    listStreams: async () => {
      historyCalls++
      return { items: [] }
    },
    listStreamSubCases: async (id) => {
      subCaseFetches.push(id)
      return { session_id: id, items: [], total: 0 }
    },
    followStreamSuite: (suiteId, _signal, onFrame) => {
      followedIds.push(suiteId)
      onSuiteFrame = onFrame
      return suiteGate.promise
    },
    cancelStreamSuite: async (suiteId) => {
      cancelledIds.push(suiteId)
      return {
        id: suiteId,
        status: 'cancelled',
        stage: 'finished',
        source: 'ai',
        title: 'AI 压力套件',
        badge: 'AI 测试',
        url: 'ws://127.0.0.1:8088/api/ws/chat',
        current_round: 1,
        total_rounds: 3,
        completed_cases: 1,
        total_cases: 3,
        cooldown_ms: 500,
        cooldown_remaining_ms: null,
        summary: '测试已取消：已完成 1 条用例',
        error_kind: 'cancelled',
        error_message: null,
      }
    },
  })
  context.after(page.unmount)

  // Seed a stale read-only replay first
  page.state.readonlyReplay.value = {
    historyId: 'old-hist',
    caseId: 'old-case',
    round: 1,
    label: '旧用例',
    status: 'closed',
  }
  page.workbenchRuns.enterReadonlyReplay('websocket', {
    historyId: 'old-hist',
    caseId: 'old-case',
    round: 1,
    label: '旧用例',
  })

  // Emit @run-bound from TaskChat
  page.state.handleRunBound({
    scope: 'websocket',
    history_id: 'suite-live-99',
    previous_history_id: 'old-hist',
    badge: 'AI 测试',
    title: 'AI 压力套件',
    forked: true,
    run_id: 'asst-run-1',
  })
  await settle()

  assert.equal(page.state.isReadOnlyReplay.value, false)
  assert.equal(page.workbenchRuns.readonlyReplay.websocket, null)
  assert.equal(page.state.activeHistoryId.value, 'suite-live-99')
  assert.equal(page.state.activeSuiteId.value, 'suite-live-99')
  assert.equal(page.workbenchRuns.activeRunId.websocket, 'suite-live-99')
  assert.equal(historyCalls, 1)
  assert.deepEqual(followedIds, ['suite-live-99'])

  // Emit cooling_down and case_done frames
  onSuiteFrame({
    type: 'cooling_down',
    suite_id: 'suite-live-99',
    current_round: 1,
    total_rounds: 3,
    cooldown_ms: 500,
    cooldown_remaining_ms: 300,
  })
  assert.equal(page.state.statusLabel.value, '轮间冷却中')

  onSuiteFrame({
    type: 'case_done',
    suite_id: 'suite-live-99',
    case_link: {
      type: 'case_link',
      scope: 'websocket',
      history_id: 'suite-live-99',
      case_id: 'sub-r1-0',
      round: 1,
      label: '第 1 轮 · 冒烟',
      status: 'passed',
      tone: 'success',
    },
  })
  await settle()
  assert.deepEqual(subCaseFetches, ['suite-live-99'])

  const runningRender = await page.render()
  assert.match(runningRender.html, /停止 AI 测试/)
  assert.match(runningRender.html, /冷却中 · 300ms/)

  // Explicit cancel button invokes cancelStreamSuite
  await page.state.cancelActiveSuite()
  assert.deepEqual(cancelledIds, ['suite-live-99'])
  assert.equal(page.state.activeSuiteId.value, null)
  assert.equal(page.workbenchRuns.activeRunId.websocket, null)
  assert.equal(page.state.suiteStatus.value.status, 'cancelled')

  // @run-completed refreshes history & sub-cases
  const beforeCompletedHistory = historyCalls
  await page.state.handleRunCompleted({ scope: 'websocket', historyId: 'suite-live-99' })
  assert.equal(historyCalls, beforeCompletedHistory + 1)
  assert.equal(subCaseFetches.length, 3)
})

test('unmount NEVER stops backend manual WS session and remount seamlessly reconnects polling from saved cursor', async () => {
  const sharedPinia = pinia.createPinia()
  let stopCalls = 0
  let firstPollSignal = null
  const firstPollGate = deferred()
  const secondPollGate = deferred()
  const readCalls = []

  // First mount: start manual session and receive event #1
  const first = mountLongConnection({
    pinia: sharedPinia,
    startStream: async (payload) => ({
      id: 'ws-persist-1',
      kind: 'websocket',
      url: payload.url,
      status: 'open',
      finished: false,
      stop_reason: null,
      error_kind: null,
      error_message: null,
      http_status: 101,
      content_type: null,
      subprotocol: null,
      close_code: null,
      close_reason: null,
      event_count: 0,
      byte_count: 0,
      sent_count: 0,
      truncated_count: 0,
      opened_at: '2026-11-27T11:00:00Z',
      closed_at: null,
    }),
    readStream: (id, after, waitMs, signal) => {
      readCalls.push({ mount: 1, id, after, waitMs })
      if (readCalls.length === 1) {
        return Promise.resolve({
          session: {
            id,
            kind: 'websocket',
            url: 'ws://127.0.0.1:8088/api/ws/chat',
            status: 'open',
            finished: false,
            stop_reason: null,
            error_kind: null,
            error_message: null,
            http_status: 101,
            content_type: null,
            subprotocol: null,
            close_code: null,
            close_reason: null,
            event_count: 1,
            byte_count: 12,
            sent_count: 0,
            truncated_count: 0,
            opened_at: '2026-11-27T11:00:00Z',
            closed_at: null,
          },
          events: [{ session_id: id, sequence: 1, direction: 'in', kind: 'text', event_name: null, data: 'hello-1', data_bytes: 7, truncated: false, elapsed_ms: 15 }],
          next_cursor: 1,
          finished: false,
          has_more: false,
        })
      }
      firstPollSignal = signal
      return firstPollGate.promise
    },
    stopStream: async () => {
      stopCalls++
      return {}
    },
  })

  first.state.form.url = 'ws://127.0.0.1:8088/api/ws/chat'
  first.state.form.scheduled = 'initial-msg'
  await first.state.start()
  await settle()
  assert.equal(first.state.events.value.length, 1)
  assert.equal(first.state.cursor.value, 1)
  assert.equal(first.workbenchRuns.activeRunId.websocket, 'ws-persist-1')

  // Trigger next poll tick so a readStream is in-flight when navigating away
  first.tick()
  assert.ok(firstPollSignal)

  // Navigate away: unmount MUST abort local poll only, NEVER call stopStream!
  first.unmount()
  assert.equal(firstPollSignal.aborted, true)
  assert.equal(stopCalls, 0)
  assert.equal(first.workbenchRuns.activeRunId.websocket, 'ws-persist-1')
  assert.equal(first.streamWorkbench.activeSessionId, 'ws-persist-1')
  assert.equal(first.streamWorkbench.events.length, 1)

  // Second mount (navigating back): restores saved form/events and reconnects live polling from cursor=1
  const second = mountLongConnection({
    pinia: sharedPinia,
    getActiveStreams: async () => ({
      items: [
        {
          id: 'ws-persist-1',
          kind: 'manual',
          source: 'manual',
          url: 'ws://127.0.0.1:8088/api/ws/chat',
          title: null,
          badge: null,
          status: 'open',
          stage: 'streaming',
          current_round: 1,
          total_rounds: 1,
          completed_cases: 0,
          total_cases: 1,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          opened_at: '2026-11-27T11:00:00Z',
        },
      ],
      manual_sessions: [
        {
          id: 'ws-persist-1',
          kind: 'manual',
          source: 'manual',
          url: 'ws://127.0.0.1:8088/api/ws/chat',
          title: null,
          badge: null,
          status: 'open',
          stage: 'streaming',
          current_round: 1,
          total_rounds: 1,
          completed_cases: 0,
          total_cases: 1,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          opened_at: '2026-11-27T11:00:00Z',
        },
      ],
      ai_suites: [],
    }),
    readStream: (id, after, waitMs) => {
      readCalls.push({ mount: 2, id, after, waitMs })
      if (waitMs === 0) {
        return Promise.resolve({
          session: {
            id,
            kind: 'websocket',
            url: 'ws://127.0.0.1:8088/api/ws/chat',
            status: 'open',
            finished: false,
            stop_reason: null,
            error_kind: null,
            error_message: null,
            http_status: 101,
            content_type: null,
            subprotocol: null,
            close_code: null,
            close_reason: null,
            event_count: 2,
            byte_count: 24,
            sent_count: 0,
            truncated_count: 0,
            opened_at: '2026-11-27T11:00:00Z',
            closed_at: null,
          },
          events: [{ session_id: id, sequence: 2, direction: 'in', kind: 'text', event_name: null, data: 'hello-2', data_bytes: 7, truncated: false, elapsed_ms: 80 }],
          next_cursor: 2,
          finished: false,
          has_more: false,
        })
      }
      return secondPollGate.promise
    },
    stopStream: async () => {
      stopCalls++
      return {}
    },
  })

  // Before onMounted even resolves, saved form and event #1 are immediately restored from Pinia
  assert.equal(second.state.form.url, 'ws://127.0.0.1:8088/api/ws/chat')
  assert.equal(second.state.form.scheduled, 'initial-msg')
  assert.equal(second.state.events.value.length, 1)

  second.triggerMounted()
  await settle()

  // Catch-up read fetched event #2 from cursor=1, then live polling resumed from cursor=2
  assert.deepEqual(
    second.state.events.value.map((e) => e.data),
    ['hello-1', 'hello-2'],
  )
  assert.equal(second.state.cursor.value, 2)
  assert.ok(readCalls.some((c) => c.mount === 2 && c.after === 1 && c.waitMs === 0))
  assert.ok(readCalls.some((c) => c.mount === 2 && c.after === 2 && c.waitMs === 1200))
  assert.equal(stopCalls, 0)
  second.unmount()
})

test('unmount NEVER cancels backend AI suite and remount automatically re-attaches followStreamSuite and refreshes on done', async () => {
  const sharedPinia = pinia.createPinia()
  let cancelCalls = 0
  let firstSuiteSignal = null
  const firstSuiteGate = deferred()
  let secondOnFrame = null
  const secondSuiteGate = deferred()
  const followCalls = []
  let historyRefreshes = 0

  const first = mountLongConnection({
    pinia: sharedPinia,
    followStreamSuite: (suiteId, signal) => {
      followCalls.push({ mount: 1, suiteId })
      firstSuiteSignal = signal
      return firstSuiteGate.promise
    },
    cancelStreamSuite: async () => {
      cancelCalls++
      return {}
    },
  })

  first.state.handleRunBound({
    scope: 'websocket',
    history_id: 'ai-suite-bg-7',
    previous_history_id: null,
    badge: 'AI 测试',
    title: '后台 WS 套件',
    forked: false,
  })
  await settle()
  assert.equal(first.state.activeSuiteId.value, 'ai-suite-bg-7')

  // Unmount: aborts local reader only, NEVER cancels backend AI suite
  first.unmount()
  assert.equal(firstSuiteSignal.aborted, true)
  assert.equal(cancelCalls, 0)
  assert.equal(first.workbenchRuns.activeRunId.websocket, 'ai-suite-bg-7')
  assert.equal(first.streamWorkbench.activeSuiteId, 'ai-suite-bg-7')

  // Remount: getActiveStreams returns active AI suite -> auto-attaches followStreamSuite
  const second = mountLongConnection({
    pinia: sharedPinia,
    listStreams: async () => {
      historyRefreshes++
      return { items: [] }
    },
    getActiveStreams: async () => ({
      items: [
        {
          id: 'ai-suite-bg-7',
          kind: 'ai_suite',
          source: 'ai',
          url: 'ws://127.0.0.1:8088/api/ws/chat',
          title: '后台 WS 套件',
          badge: 'AI 测试',
          status: 'running',
          stage: 'executing',
          current_round: 2,
          total_rounds: 2,
          completed_cases: 1,
          total_cases: 2,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          opened_at: '2026-11-27T11:05:00Z',
        },
      ],
      manual_sessions: [],
      ai_suites: [
        {
          id: 'ai-suite-bg-7',
          kind: 'ai_suite',
          source: 'ai',
          url: 'ws://127.0.0.1:8088/api/ws/chat',
          title: '后台 WS 套件',
          badge: 'AI 测试',
          status: 'running',
          stage: 'executing',
          current_round: 2,
          total_rounds: 2,
          completed_cases: 1,
          total_cases: 2,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          opened_at: '2026-11-27T11:05:00Z',
        },
      ],
    }),
    followStreamSuite: (suiteId, _signal, onFrame) => {
      followCalls.push({ mount: 2, suiteId })
      secondOnFrame = onFrame
      return secondSuiteGate.promise
    },
    cancelStreamSuite: async () => {
      cancelCalls++
      return {}
    },
  })

  second.triggerMounted()
  await settle()

  assert.deepEqual(followCalls, [
    { mount: 1, suiteId: 'ai-suite-bg-7' },
    { mount: 2, suiteId: 'ai-suite-bg-7' },
  ])
  assert.equal(second.state.isSuiteRunning.value, true)
  const beforeDoneRefreshes = historyRefreshes

  // Complete the AI suite on the reconnected stream
  const doneFrame = {
    type: 'done',
    suite_id: 'ai-suite-bg-7',
    id: 'ai-suite-bg-7',
    status: 'completed',
    stage: 'finished',
    source: 'ai',
    title: '后台 WS 套件',
    badge: 'AI 测试',
    url: 'ws://127.0.0.1:8088/api/ws/chat',
    current_round: 2,
    total_rounds: 2,
    completed_cases: 2,
    total_cases: 2,
    cooldown_ms: 500,
    cooldown_remaining_ms: null,
    summary: '共计划 2 轮 2 条 WebSocket 用例，全部通过。',
    error_kind: null,
    error_message: null,
  }
  secondOnFrame(doneFrame)
  secondSuiteGate.resolve(doneFrame)
  await settle()

  assert.equal(second.state.activeSuiteId.value, null)
  assert.equal(second.workbenchRuns.activeRunId.websocket, null)
  assert.equal(second.state.suiteSummaryText.value, '共计划 2 轮 2 条 WebSocket 用例，全部通过。')
  assert.ok(historyRefreshes > beforeDoneRefreshes)
  assert.equal(cancelCalls, 0)
  second.unmount()
})

test('fresh mount (page reload) auto-restores inputs and resumes polling for an active manual session', async (context) => {
  const inputRequests = []
  const pollGate = deferred()
  const page = mountLongConnection({
    getActiveStreams: async () => ({
      items: [],
      manual_sessions: [
        {
          id: 'ws-reload-1',
          kind: 'manual',
          source: 'manual',
          url: 'ws://127.0.0.1:8088/api/ws/reload',
          title: null,
          badge: null,
          status: 'open',
          stage: 'streaming',
          current_round: 1,
          total_rounds: 1,
          completed_cases: 0,
          total_cases: 1,
          cooldown_ms: 500,
          cooldown_remaining_ms: null,
          opened_at: '2026-11-27T11:10:00Z',
        },
      ],
      ai_suites: [],
    }),
    readStreamInputs: async (id) => {
      inputRequests.push(id)
      return {
        url: 'ws://127.0.0.1:8088/api/ws/reload',
        request_headers: { 'X-Env': 'dev' },
        subprotocols: ['proto-a'],
        scheduled_messages: ['hello'],
      }
    },
    readStream: (id, after, waitMs) => {
      if (waitMs === 0) {
        return Promise.resolve({
          session: {
            id,
            kind: 'websocket',
            url: 'ws://127.0.0.1:8088/api/ws/reload',
            status: 'open',
            finished: false,
            stop_reason: null,
            error_kind: null,
            error_message: null,
            http_status: 101,
            content_type: null,
            subprotocol: 'proto-a',
            close_code: null,
            close_reason: null,
            event_count: 1,
            byte_count: 10,
            sent_count: 1,
            truncated_count: 0,
            opened_at: '2026-11-27T11:10:00Z',
            closed_at: null,
          },
          events: [{ session_id: id, sequence: 1, direction: 'out', kind: 'text', event_name: null, data: 'hello', data_bytes: 5, truncated: false, elapsed_ms: 5 }],
          next_cursor: 1,
          finished: false,
          has_more: false,
        })
      }
      assert.equal(after, 1)
      return pollGate.promise
    },
  })
  context.after(page.unmount)

  page.triggerMounted()
  await settle()

  assert.deepEqual(inputRequests, ['ws-reload-1'])
  assert.equal(page.state.form.url, 'ws://127.0.0.1:8088/api/ws/reload')
  assert.equal(page.state.form.subprotocols, 'proto-a')
  assert.equal(page.state.form.scheduled, 'hello')
  assert.equal(page.state.activeHistoryId.value, 'ws-reload-1')
  assert.equal(page.state.isLive.value, true)
  assert.equal(page.state.events.value.length, 1)
})

test('LongConnection source invariants enforce safe unmount without backend kill and complete TaskChat wiring', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/views/LongConnection.vue'), 'utf8')
  const unmountBlock = source.slice(source.indexOf('onBeforeUnmount('), source.indexOf('</script>'))
  assert.match(unmountBlock, /stopPolling\(\)/)
  assert.match(unmountBlock, /stopSuiteObserver\(\)/)
  assert.doesNotMatch(unmountBlock, /stopStream/)
  assert.doesNotMatch(unmountBlock, /cancelStreamSuite/)

  assert.match(source, /<TaskChat[\s\S]*scope="websocket"[\s\S]*:execution-id="activeHistoryId"[\s\S]*:draft-version="draftVersion"[\s\S]*:current-test="assistantContext"[\s\S]*:settings="assistantSettings"[\s\S]*:active-case-id="readonlyReplay\?\.caseId \?\? null"[\s\S]*@run-bound="handleRunBound"[\s\S]*@run-completed="handleRunCompleted"[\s\S]*@select-case="handleSelectCase"/)
})
