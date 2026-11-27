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
const taskResultExports = {}
new Function('exports', 'require', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/taskResults.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(taskResultExports, () => resultExports)
const prefixExports = {}
new Function('exports', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/urlPrefixes.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(prefixExports)
const formatExports = {}
new Function('exports', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/format.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(formatExports)
const copyExports = {}
new Function('exports', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/copy.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(copyExports)
const assistantContextExports = {}
new Function('exports', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/assistantContext.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(assistantContextExports)
const chatHistoryJs = ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/chatHistory.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText
function loadChatHistory(storage) {
  const exports = {}
  new Function('exports', 'window', chatHistoryJs)(exports, { localStorage: storage })
  return exports
}
const taskExports = {}
new Function('exports', 'require', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/api/tasks.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(taskExports, () => clientExports)

// Execute the production SFC setup scripts with real Vue reactivity. Only the
// API, lifecycle registration and browser clock are substituted; no HTTP/model.
globalThis.fetch = () => { throw new Error('Network is prohibited in frontend tests') }

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('previously linked test chats remain readable and can be removed with the test', () => {
  const storage = memoryStorage({
    'api-agent.chat.console.chat-one': '[{"role":"user","content":"你好"}]',
    'api-agent.chat-execution.console.request-one': 'chat-one',
    'api-agent.chat.console.chat-only': '[]',
  })
  const { chatTranscriptKey, removeTestChat } = loadChatHistory(storage)
  assert.equal(chatTranscriptKey('console', 'request-one'), 'api-agent.chat.console.chat-one')
  removeTestChat('console', 'request-one')
  assert.equal(storage.getItem('api-agent.chat-execution.console.request-one'), null)
  assert.equal(storage.getItem('api-agent.chat.console.chat-one'), null)
  assert.equal(storage.getItem('api-agent.chat.console.chat-only'), '[]')
})

function load(name, names, mocks = {}, directory = 'views') {
  const sfc = fs.readFileSync(path.join(__dirname, '../src', directory, `${name}.vue`), 'utf8')
  const source = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/^\s*import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/gm, '')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const timers = new Map()
  const unmount = []
  let timerId = 0
  const scope = vue.effectScope()
  const { storage = memoryStorage(), ...dependencies } = mocks
  const env = {
    ...vue,
    ...copyExports,
    ...assistantContextExports,
    ...loadChatHistory(storage),
    ...resultExports,
    ...taskResultExports,
    ...prefixExports,
    isSseHistory: formatExports.isSseHistory,
    taskHistoryLabel: taskExports.taskHistoryLabel,
    ApiError: clientExports.ApiError,
    onMounted: () => {},
    onBeforeUnmount: fn => unmount.push(fn),
    onBeforeRouteLeave: () => {},
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
    readStreamInputs: async () => ({ url: 'ws://127.0.0.1:8010/ws', request_headers: {}, subprotocols: [], scheduled_messages: [] }),
    // AutoPane 用别名导入 HTTP 状态码色调（内部另有同名生命周期函数），
    // 测试原先只桩了 format* 系列，别名缺失会让断言证据渲染直接抛错。
    httpStatusTone: value => (value >= 500 ? 'danger' : value >= 400 ? 'warning'
      : value >= 300 ? 'info' : value >= 200 ? 'success' : 'neutral'),
    window: {
      localStorage: storage,
      addEventListener: () => {},
      setTimeout: fn => { timers.set(++timerId, { fn, repeat: false }); return timerId },
      clearTimeout: id => timers.delete(id),
      setInterval: fn => { timers.set(++timerId, { fn, repeat: true }); return timerId },
      clearInterval: id => timers.delete(id),
    },
    ...dependencies,
  }
  if (sfc.includes("'@/lib/testSettings'")) {
    const settingsSource = fs.readFileSync(path.join(__dirname, '../src/lib/testSettings.ts'), 'utf8')
    const settingsJs = ts.transpileModule(settingsSource, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText
    const settingsExports = {}
    new Function('require', 'exports', 'window', settingsJs)(() => vue, settingsExports, env.window)
    Object.assign(env, settingsExports)
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
    `${js};return {${[exposed, `templateState: {${bindings.join(',')}}`].filter(Boolean).join(',')}}`)(...Object.values(env)))
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
      for (const component of sfc.matchAll(/import\s+(\w+)(?:,\s*\{[^}]*\})?\s+from\s+['"][^'"]+\.vue['"]/g)) {
        if (['TaskSummary', 'AiMarkdown'].includes(component[1])) {
          app.component(component[1], { props: ['text'], setup: props => () => vue.h('div', props.text) })
        } else {
          app.component(component[1], { setup: (_, { slots }) => () => vue.h('div',
            Object.values(slots).flatMap(slot => slot({ item: {} }))) })
        }
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

test('the console keeps the response split and SSE events across stream completion', async context => {
  const final = deferred()
  let notify
  const page = load('RequestConsole', ['send', 'result', 'tab'], {
    sendConsoleRequest: (_request, onProgress) => { notify = onProgress; return final.promise },
    listConsoleHistory: async () => ({ items: [] }),
    statusTone: () => 'neutral',
    looksLikeJson: () => false,
    formatBytes: value => `${value} B`,
  })
  context.after(page.dispose)
  const responseSplit = /class="response-split"[\s\S]*class="response-content code-block scroll-y"[\s\S]*class="response-headers"/
  const initial = await page.render()
  assert.match(initial, responseSplit)
  assert.match(initial, /暂无响应头/)
  page.tab.value = 'headers'
  const pending = page.send()
  notify({ type: 'sse_start' })
  const waiting = await page.render()
  assert.match(waiting, responseSplit)
  assert.match(waiting, /正在接收 SSE · 0 个事件/)
  assert.match(waiting, /等待响应头/)
  notify({ type: 'sse_event', event: { event: 'message', data: '首条数据' } })
  assert.match(await page.render(), /正在接收 SSE · 1 个事件[\s\S]*首条数据/)
  notify({ type: 'sse_event', event: { event: 'message', data: '第二条数据' } })
  assert.match(await page.render(), /正在接收 SSE · 2 个事件[\s\S]*首条数据[\s\S]*第二条数据/)
  assert.equal(page.result.value, null)
  final.resolve({ ok: true, elapsed_ms: 100, response: {
    status: 200, body: 'data: 首条数据\n\ndata: 第二条数据\n\n', body_bytes: 44, body_truncated: true,
    headers: { 'content-type': ['text/event-stream'] }, redirects: [],
    sse: { events: ['首条数据', '第二条数据'].map(data => ({ event: 'message', data })), stop_reason: 'observation_complete' },
  } })
  await pending
  const completed = await page.render()
  assert.match(completed, responseSplit)
  assert.match(completed, /class="sse-events"[\s\S]*首条数据[\s\S]*第二条数据[\s\S]*class="response-headers"[\s\S]*text\/event-stream/)
  assert.doesNotMatch(completed, /class="raw-response"|等待响应头/)
  assert.equal(page.tab.value, 'headers')
})

test('clearing the console during SSE hides late events and still refreshes saved history', async context => {
  const final = deferred()
  let notify, signal
  const record = { id: 'saved', method: 'GET', url: 'http://127.0.0.1:9100/sse', request_headers: {}, body_mode: 'none', ok: true, response_preview: 'saved response' }
  const page = load('RequestConsole', ['send', 'clearResponse', 'restore', 'result', 'liveEvents', 'history'], {
    sendConsoleRequest: (_input, onProgress, requestSignal) => {
      notify = onProgress
      signal = requestSignal
      return final.promise
    },
    listConsoleHistory: async () => ({ items: [record] }),
  })
  context.after(page.dispose)
  const pending = page.send()
  notify({ type: 'sse_event', event: { event: 'message', data: 'first' } })
  page.clearResponse()
  notify({ type: 'sse_event', event: { event: 'message', data: 'late' } })
  assert.equal(signal.aborted, false)
  assert.equal(page.liveEvents.value, null)
  final.resolve({ ok: true })
  await pending
  assert.equal(page.result.value, null)
  assert.equal(page.history.value[0].id, 'saved')
  page.restore(page.history.value[0])
  assert.equal(page.result.value.response.body, 'saved response')
})

test('console settings apply to different requests and survive reopening the page', async context => {
  const initial = { timeoutMs: 60000, sseDurationMs: 30000, sseMaxEvents: 1000, followRedirects: true }
  const storage = memoryStorage({ 'api-agent.console.settings': JSON.stringify(initial) })
  const requests = []
  const mocks = {
    storage,
    sendConsoleRequest: async request => { requests.push(request); return { ok: true } },
    listConsoleHistory: async () => ({ items: [] }),
  }
  const payloadSettings = ({ timeout_ms, sse_duration_ms, sse_max_events, follow_redirects }) =>
    [timeout_ms, sse_duration_ms, sse_max_events, follow_redirects]
  const page = load('RequestConsole', ['send', 'restore', 'consoleSettings'], mocks)
  context.after(page.dispose)
  await page.send()
  assert.deepEqual(payloadSettings(requests[0]), [60000, 30000, 1000, true])

  Object.assign(page.consoleSettings, { timeoutMs: 40000, sseDurationMs: 20000, sseMaxEvents: 600, followRedirects: false })
  const historicalUrl = 'http://127.0.0.1:9000/old'
  page.restore({ id: 'old', method: 'GET', url: historicalUrl, request_headers: {}, body_mode: 'none', ok: true })
  await page.send()
  assert.equal(requests[1].url, historicalUrl)
  assert.deepEqual(payloadSettings(requests[1]), [40000, 20000, 600, false])

  const reopened = load('RequestConsole', ['send'], mocks)
  context.after(reopened.dispose)
  await reopened.send()
  assert.notEqual(requests[2].url, historicalUrl)
  assert.deepEqual(payloadSettings(requests[2]), [40000, 20000, 600, false])
})

test('WebSocket settings start collapsed and carry their values into new sessions', async context => {
  const storage = memoryStorage({ 'api-agent.websocket.send-interval-ms': '1200' })
  const sessions = []
  const finishedSession = { id: 'session', finished: true }
  const mocks = {
    storage,
    startStream: async request => { sessions.push(request); return finishedSession },
    listStreams: async () => ({ items: [] }),
    readStream: async () => ({ session: finishedSession, events: [], has_more: false }),
    formatBytes: () => '0 B',
    describeStopReason: () => '',
  }
  const page = load('LongConnection', ['start', 'replay', 'form', 'webSocketSettings'], mocks)
  context.after(page.dispose)
  await page.start()
  assert.equal(sessions[0].send_interval_ms, 1200)
  const html = await page.render()
  const settings = html.match(/<details\b(?![^>]*\sopen(?:\s|=|>))[^>]*>\s*<summary>设置<\/summary>([\s\S]*?)<\/details>/)
  assert.ok(settings)

  Object.assign(page.webSocketSettings, {
    sendIntervalMs: 0, maxEvents: 350, connectTimeoutMs: 8000, maxDurationMs: 45000, pingIntervalMs: 0,
  })
  finishedSession.settings = { send_interval_ms: 500, max_events: 200, connect_timeout_ms: 10000 }
  await page.replay({ id: 'old' })
  page.form.url = 'ws://127.0.0.1:9000/other'
  await page.start()
  const reopened = load('LongConnection', ['start'], mocks)
  context.after(reopened.dispose)
  await reopened.start()
  assert.notEqual(sessions[0].url, sessions[1].url)
  const payloadSettings = ({ send_interval_ms, max_events, connect_timeout_ms, max_duration_ms, ping_interval_ms }) =>
    [send_interval_ms, max_events, connect_timeout_ms, max_duration_ms, ping_interval_ms]
  assert.deepEqual(sessions.map(payloadSettings), [
    [1200, 200, 60000, 900000, 20000],
    [0, 350, 8000, 45000, 0],
    [0, 350, 8000, 45000, 0],
  ])
})

const taskRecord = (id, status = 'running') => ({ id, status, stage: 'executing',
  created_at: '2026-11-16T00:00:00Z', snapshot: { cases: [], warnings: [] }, results: [], summary: null })

test('new tests clear workbench state and ignore replies from the previous test', async context => {
  const pendingHttp = deferred()
  const pendingSocket = deferred()
  const pendingCase = deferred()
  const http = load('RequestConsole', ['send', 'newTest', 'form', 'result'], {
    sendConsoleRequest: () => pendingHttp.promise,
    listConsoleHistory: async () => ({ items: [] }),
  })
  const socket = load('LongConnection', ['start', 'newTest', 'form', 'session', 'outgoing'], {
    startStream: () => pendingSocket.promise,
    listStreams: async () => ({ items: [] }),
  })
  const task = load('TestCasesView', ['submit', 'newTest', 'origin', 'catalog', 'run'], {
    startTask: () => pendingCase.promise,
    listTasks: async () => ({ items: [] }),
  })
  for (const page of [http, socket, task]) context.after(page.dispose)
  task.origin.value = 'http://127.0.0.1:9100'
  task.catalog.value = { cases: [], endpoints: [{ path: '/health' }], warnings: [] }
  socket.outgoing.value = 'old message'
  const pending = [http.send(), socket.start(), task.submit()]
  for (const page of [http, socket, task]) page.newTest()
  pendingHttp.resolve({ ok: true })
  pendingSocket.resolve({ id: 'prior-socket', finished: true })
  pendingCase.resolve(taskRecord('prior-case', 'completed'))
  await Promise.all(pending)
  assert.equal(http.form.url, '')
  assert.equal(http.result.value, null)
  assert.equal(socket.form.url, '')
  assert.equal(socket.session.value, null)
  assert.equal(socket.outgoing.value, '')
  assert.equal(task.origin.value, '')
  assert.equal(task.catalog.value, null)
  assert.equal(task.run.value, null)
})

test('fresh workbench settings match the defaults in system settings', context => {
  const page = load('TestCasesView', ['taskSettings'], { storage: memoryStorage() })
  context.after(page.dispose)
  assert.deepEqual({ ...page.taskSettings }, { allowWrites: true, testsPerUrl: 3, observationMs: 30000, wallTimeSeconds: 3600, cooldownMs: 500 })
  const http = load('RequestConsole', ['consoleSettings'], { storage: memoryStorage() })
  context.after(http.dispose)
  assert.deepEqual({ ...http.consoleSettings }, { sseDurationMs: 30000, sseMaxEvents: 50, timeoutMs: 60000, cooldownMs: 500, followRedirects: false })
  const websocket = load('LongConnection', ['webSocketSettings'], { storage: memoryStorage() })
  context.after(websocket.dispose)
  assert.deepEqual({ ...websocket.webSocketSettings }, {
    sendIntervalMs: 500, maxEvents: 200, connectTimeoutMs: 60000, maxDurationMs: 900000, pingIntervalMs: 20000, cooldownMs: 500,
  })
})

test('settings edits clamp to the nearest boundary and report the correction', context => {
  const storage = memoryStorage()
  const notifications = []
  for (const [group, key, min, max] of [
    ['console', 'timeoutMs', 100, 60000], ['websocket', 'sendIntervalMs', 0, 60000], ['task', 'wallTimeSeconds', 10, 3600],
  ]) {
    const page = load('TestSettingsFields', ['updateNumber', 'fields'], {
      storage, defineProps: () => ({ group }), useToastStore: () => ({ push: (...args) => notifications.push(args) }),
    }, 'components')
    context.after(page.dispose)
    const field = page.fields.value.find(item => item.key === key)
    for (const [raw, expected] of [[min - 1, min], [max + 1, max]]) {
      const input = { valueAsNumber: raw, value: String(raw) }
      page.updateNumber({ currentTarget: input }, field)
      assert.equal(input.value, String(expected))
      assert.equal(JSON.parse(storage.getItem('api-agent.test-settings'))[group][key], expected)
      assert.match(notifications.at(-1)[1], /超出范围/)
    }
  }
})

test('history selection fills the original inputs and leaves execution to the user', async context => {
  const requests = []
  const fullBody = JSON.stringify({ question: 'a'.repeat(9000) })
  const http = load('RequestConsole', ['send', 'restoreById', 'form'], {
    readConsoleHistory: async id => ({ id, method: 'POST', url: 'http://127.0.0.1:9100/ask?q=1',
      request_headers: {}, body_mode: 'json', request_body: fullBody, body_preview: fullBody.slice(0, 8192), ok: true }),
    sendConsoleRequest: async input => { requests.push(input); return { ok: true } },
    listConsoleHistory: async () => ({ items: [] }),
  })
  const socket = load('LongConnection', ['start', 'replay', 'form'], {
    readStreamInputs: async () => ({ url: 'ws://127.0.0.1:9100/ws', request_headers: {},
      subprotocols: ['demo'], scheduled_messages: ['subscribe', 'ping'] }),
    readStream: async () => ({ session: { id: 'old-socket', finished: true }, events: [], has_more: false }),
    startStream: async input => { requests.push(input); return { id: 'new-socket', finished: true } },
    listStreams: async () => ({ items: [] }),
  })
  const snapshots = [{ name: '状态', method: 'GET', protocol: 'http', path: '/health', source: 'document', assertions: [] }]
  const original = { ...taskRecord('old-task', 'completed'), snapshot: { cases: snapshots, origin: 'http://127.0.0.1:9100/api', warnings: [] } }
  const repeated = { ...original, id: 'new-task' }
  const task = load('TestCasesView', ['selectHistory', 'submit', 'origin', 'catalog', 'run'], {
    readTask: async id => id === original.id ? original : repeated,
    rerunTask: async (id, options) => { requests.push({ id, ...options }); return repeated },
    listTasks: async () => ({ items: [original, repeated] }),
  })
  for (const page of [http, socket, task]) context.after(page.dispose)
  await http.restoreById('old-http')
  await socket.replay({ id: 'old-socket' })
  await task.selectHistory('old-task')
  assert.equal(http.form.body, fullBody)
  assert.equal(socket.form.scheduled, 'subscribe\nping')
  assert.equal(task.origin.value, original.snapshot.origin)
  assert.equal(task.catalog.value, null)
  assert.deepEqual(requests, [])
  await http.send()
  await socket.start()
  await task.submit()
  assert.equal(requests[0].body, fullBody)
  assert.deepEqual(requests[1].subprotocols, ['demo'])
  assert.deepEqual(requests[1].send, ['subscribe', 'ping'])
  assert.equal(requests[2].id, 'old-task')
  assert.equal(task.run.value.id, 'new-task')
})

test('editing task history submits reviewed inputs as a separate rerun', async context => {
  const requests = []
  const cases = [
    { name: '修改前的用例', protocol: 'http', method: 'POST', path: '/echo', source: 'generated', body: { q: 'old' }, assertions: [] },
    { name: '契约示例', protocol: 'http', method: 'POST', path: '/echo', source: 'generated', body: { q: 'example' }, assertions: [] },
  ]
  const original = {
    ...taskRecord('saved-task', 'completed'),
    title: '历史样例',
    snapshot: {
      cases,
      origin: 'http://127.0.0.1:9100',
      warnings: [],
      operations: [{ path: '/echo', example_index: 1, case_indices: [0, 1] }],
    },
  }
  const repeated = { ...original, id: 'edited-task' }
  const task = load('TestCasesView', [
    'selectHistory', 'submit', 'changeDraft', 'replayDraft', 'replayEdits', 'run',
  ], {
    readTask: async id => id === original.id ? original : repeated,
    rerunTask: async (id, options) => { requests.push({ id, ...options }); return repeated },
    listTasks: async () => ({ items: [original, repeated] }),
  })
  context.after(task.dispose)
  await task.selectHistory(original.id)
  task.changeDraft({ kind: 'history', index: 0 }, { ...cases[0], name: '修改后的用例', body: { q: 'new' } })
  task.changeDraft({ kind: 'history', index: 1 }, { ...cases[1], body: { q: 'edited example' } })
  assert.equal(task.replayDraft.value.cases[0].body.q, 'new')
  assert.equal(task.replayDraft.value.cases[1].body.q, 'edited example')
  assert.deepEqual(original.snapshot.cases, cases)
  await task.submit()
  assert.equal(requests[0].id, original.id)
  assert.equal(requests[0].case_edits[0].name, '修改后的用例')
  assert.equal(requests[0].case_edits[0].body.q, 'new')
  assert.equal(requests[0].case_edits[1].body.q, 'edited example')
  assert.equal(task.run.value.id, repeated.id)
})

test('regeneration sends only checked operations after removing one from history', async context => {
  const requests = []
  const cases = ['/one', '/two', '/three'].map(path => ({
    name: path, protocol: 'http', method: 'GET', path, source: 'generated', kind: 'contract', assertions: [],
  }))
  const original = { ...taskRecord('history-for-selection', 'completed'), snapshot: {
    cases, origin: 'http://127.0.0.1:9100', warnings: [],
    document: { filename: 'fixture.json' },
    operations: cases.map(({ path }, index) => ({ path, example_index: index, case_indices: [index] })),
  } }
  const page = load('TestCasesView', [
    'selectHistory', 'removeOperation', 'beginSelection', 'toggleOperation', 'prepareDraft', 'catalog', 'selecting',
  ], {
    readTask: async () => original,
    readTaskDocument: async () => '{"endpoints":[{"path":"/one"}]}',
    listTasks: async () => ({ items: [original] }),
    regenerateTests: async (draft, indices, count, document) => {
      requests.push({ paths: draft.endpoints.map(item => item.path), indices, count, document })
      return draft
    },
  })
  context.after(page.dispose)
  await page.selectHistory(original.id)
  page.removeOperation(0)
  page.beginSelection()
  page.toggleOperation(taskResultExports.operationDraftKey(cases[2]))
  await page.prepareDraft('selected')
  assert.deepEqual(requests[0].paths, ['/two', '/three'])
  assert.deepEqual(requests[0].indices, [1])
  assert.equal(requests[0].document.filename, 'fixture.json')
  assert.equal(page.selecting.value, false)
  assert.equal(original.snapshot.cases.length, 3)
})

test('a reply in progress does not lock the test workspace', async context => {
  const requests = []
  const original = taskRecord('reviewed-task', 'completed')
  const page = load('TestCasesView', ['run', 'chatting', 'selectHistory', 'removeHistory', 'newTest', 'submit'], {
    readTask: async id => { requests.push(['read', id]); return original },
    deleteTask: async id => { requests.push(['delete', id]) },
    listTasks: async () => ({ items: [original] }),
  })
  context.after(page.dispose)
  page.run.value = original
  page.chatting.value = true
  await page.selectHistory('another-task')
  await page.removeHistory(original.id)
  page.newTest()
  await page.submit()
  assert.deepEqual(requests, [['read', 'another-task'], ['delete', original.id]])
  assert.equal(page.run.value, null)
})

test('the assistant uses the saved model, sends without a task and blocks overlapping replies', async context => {
  const reply = deferred()
  const requests = []
  const storage = memoryStorage()
  let savedModel = 'system-model'
  const assistant = load('TaskChat', ['readModel', 'selectModel', 'send', 'input', 'model', 'sending'], {
    storage,
    defineProps: () => ({ scope: 'tasks', executionId: 'history-one' }),
    useId: () => 'assistant-model-menu',
    getLLMConfig: async () => ({ model: savedModel }),
    selectLLMModel: async model => ({ model: (savedModel = model) }),
    chatWithAssistant: (taskId, messages, onText) => {
      requests.push({ taskId, messages, model: savedModel })
      onText('回复片段')
      return reply.promise
    },
  }, 'components')
  context.after(assistant.dispose)
  await assistant.readModel()
  assert.equal(assistant.model.value, 'system-model')
  await assistant.selectModel('new-model')
  assert.equal(savedModel, 'new-model')
  assistant.input.value = '你好'
  const sending = assistant.send()
  assert.equal(assistant.sending.value, true)
  assert.match(await assistant.render(), /回复片段/)
  assistant.input.value = '下一条草稿'
  await assistant.send()
  assert.deepEqual(requests, [{ taskId: undefined, messages: [{ role: 'user', content: '你好' }], model: 'new-model' }])
  reply.resolve('完整回复')
  await sending
  assert.match(await assistant.render(), /完整回复/)
  assert.deepEqual(JSON.parse(storage.getItem('api-agent.chat.tasks.history-one')), [
    { role: 'user', content: '你好' }, { role: 'assistant', content: '完整回复' },
  ])
  assert.equal(assistant.input.value, '下一条草稿')
  assert.equal(assistant.sending.value, false)
})

test('ordinary assistant turns support follow-ups without writing browser history', async context => {
  const storage = memoryStorage()
  const requests = []
  const props = vue.reactive({ scope: 'console', draftVersion: 0 })
  const options = {
    storage,
    defineProps: () => props,
    useId: () => 'chat-test',
    chatWithAssistant: async (_taskId, messages) => {
      requests.push(messages)
      return `答复：${messages.at(-1).content}`
    },
  }
  const assistant = load('TaskChat', ['send', 'input', 'turns'], options, 'components')
  context.after(assistant.dispose)
  assistant.input.value = '你好'
  await assistant.send()
  assistant.input.value = '再补充一下'
  await assistant.send()
  assert.equal(assistant.turns.value.length, 4)
  assert.deepEqual(requests[1].map(turn => turn.content), ['你好', '答复：你好', '再补充一下'])
  assert.equal(storage.length, 0)
  const reopened = load('TaskChat', ['turns'], options, 'components')
  context.after(reopened.dispose)
  assert.deepEqual(reopened.turns.value, [])
  props.draftVersion++
  await vue.nextTick()
  assert.deepEqual(assistant.turns.value, [])
})

test('an in-flight reply never enters a different test history or recreates a deleted transcript', async context => {
  const storage = memoryStorage()
  const props = vue.reactive({ scope: 'tasks', draftVersion: 0, executionId: null, taskId: undefined })
  let reply = deferred()
  const taskIds = []
  const assistant = load('TaskChat', ['send', 'input', 'turns'], {
    storage, defineProps: () => props, useId: () => 'chat-test',
    chatWithAssistant: taskId => { taskIds.push(taskId); return reply.promise },
  }, 'components')
  context.after(assistant.dispose)
  assistant.input.value = '普通对话'
  const draft = assistant.send()
  Object.assign(props, { executionId: 'test-one', taskId: 'test-one' })
  await vue.nextTick()
  reply.resolve('普通回复')
  await draft
  assert.equal(storage.length, 0)
  assert.deepEqual(assistant.turns.value, [])

  reply = deferred()
  assistant.input.value = '测试一的问题'
  const testOne = assistant.send()
  Object.assign(props, { executionId: 'test-two', taskId: 'test-two' })
  await vue.nextTick()
  reply.resolve('测试一的回复')
  await testOne
  assert.equal(storage.getItem('api-agent.chat.tasks.test-two'), null)
  assert.deepEqual(assistant.turns.value, [])
  Object.assign(props, { executionId: 'test-one', taskId: 'test-one' })
  await vue.nextTick()
  assert.deepEqual(assistant.turns.value.map(turn => turn.content), ['测试一的问题', '测试一的回复'])

  reply = deferred()
  assistant.input.value = '另一个问题'
  const deleted = assistant.send()
  loadChatHistory(storage).removeTestChat('tasks', 'test-one')
  Object.assign(props, { executionId: null, taskId: undefined })
  reply.resolve('迟到的回复')
  await deleted
  assert.deepEqual(taskIds, [undefined, 'test-one', 'test-one'])
  assert.equal(storage.length, 0)
  assert.deepEqual(assistant.turns.value, [])
})

test('shared AI Markdown renders streamed sections and escapes model HTML', async context => {
  const props = vue.reactive({ text: '## 概览\n- **重点**与`示例`\n\n```json\n{"ok": true}' })
  const markdown = load('AiMarkdown', [], { defineProps: () => props }, 'components')
  context.after(markdown.dispose)
  const partial = await markdown.render()
  assert.match(partial, /<h3>概览<\/h3>/)
  assert.match(partial, /<ul>[\s\S]*<strong>重点<\/strong>[\s\S]*<code>示例<\/code>/)
  assert.match(partial, /<pre><code>\{&quot;ok&quot;: true\}<\/code><\/pre>/)
  props.text += '\n```\n<img src="fixture">'
  const complete = await markdown.render()
  assert.match(complete, /&lt;img/)
  assert.doesNotMatch(complete, /<img /)
})

test('assistant version mismatch preserves the management request ID', async () => {
  const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/api/tasks.ts'), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const assistantApi = {}
  let requests = 0
  new Function('exports', 'require', compiled)(assistantApi, () => ({
    ApiError: clientExports.ApiError,
    request: async (url, options) => {
      assert.equal(url, '/api/tasks/chat')
      assert.equal(options.method, 'POST')
      requests++
      throw new clientExports.ApiError(405, 'http_error', 'Method is not allowed', [], 'fixture-id')
    },
  }))
  await assert.rejects(assistantApi.chatWithAssistant(undefined, [{ role: 'user', content: '你好' }], () => {}),
    error => error.code === 'assistant_route_unavailable' && error.requestId === 'fixture-id' && /重新启动/.test(error.message))
  assert.equal(requests, 1)
})

test('task limits and write permission survive history selection and reopening with the other test settings', async context => {
  const storage = memoryStorage({
    'api-agent.console.settings': '{"timeoutMs":18000,"followRedirects":true}',
    'api-agent.websocket.send-interval-ms': '0',
  })
  const requests = []
  const mocks = {
    storage,
    startTask: async request => { requests.push(request); return taskRecord('new', 'completed') },
    rerunTask: async (_id, request) => { requests.push(request); return taskRecord('rerun', 'completed') },
    listTasks: async () => ({ items: [] }),
    readTask: async id => ({ ...taskRecord(id, 'completed'), snapshot: { cases: [{ path: '/health' }], origin: 'http://127.0.0.1:9100', warnings: [] }, allow_writes: false, observation_ms: 1000, wall_time_seconds: 300 }),
  }
  const open = () => {
    const page = load('TestCasesView', ['submit', 'selectHistory', 'origin', 'catalog', 'taskSettings'], mocks)
    context.after(page.dispose)
    page.origin.value = 'http://127.0.0.1:9100'
    page.catalog.value = { cases: [], endpoints: [{ path: '/health' }], warnings: [] }
    return page
  }
  const first = open()
  assert.equal(first.taskSettings.allowWrites, true)
  Object.assign(first.taskSettings, { allowWrites: true, observationMs: 4800, wallTimeSeconds: 75 })
  await first.selectHistory('old')
  await first.submit()
  const reopened = open()
  await reopened.selectHistory('old')
  await reopened.submit()
  reopened.taskSettings.allowWrites = false
  await open().submit()
  assert.deepEqual(requests.map(({ allow_writes, observation_ms, wall_time_seconds }) =>
    [allow_writes, observation_ms, wall_time_seconds]), [[true, 4800, 75], [true, 4800, 75], [false, 4800, 75]])
  const saved = JSON.parse(storage.getItem('api-agent.test-settings'))
  assert.equal(saved.console.timeoutMs, 18000)
  assert.equal(saved.console.followRedirects, true)
  assert.equal(saved.websocket.sendIntervalMs, 0)
})

test('the three workbenches submit a saved prefix with the editable suffix', async context => {
  const submitted = []
  const http = load('RequestConsole', ['send', 'selectUrlPrefix', 'form'], {
    sendConsoleRequest: async request => { submitted.push(request.url); return { ok: true } },
    listConsoleHistory: async () => ({ items: [] }),
  })
  const socket = load('LongConnection', ['start', 'selectUrlPrefix', 'form'], {
    startStream: async request => { submitted.push(request.url); return { id: 'socket', finished: true } },
    listStreams: async () => ({ items: [] }),
  })
  const task = load('TestCasesView', ['submit', 'selectUrlPrefix', 'origin', 'catalog'], {
    startTask: async request => { submitted.push(request.origin); return taskRecord('task', 'completed') },
    listTasks: async () => ({ items: [] }),
    readTask: async () => taskRecord('task', 'completed'),
  })
  for (const page of [http, socket, task]) context.after(page.dispose)
  http.selectUrlPrefix('http://127.0.0.1:8000/api')
  http.form.url = '/items?page=2'
  await http.send()
  socket.selectUrlPrefix('ws://127.0.0.1:9000/socket')
  socket.form.url = 'events'
  await socket.start()
  task.selectUrlPrefix('http://127.0.0.1:8000/api')
  task.origin.value = '/v2'
  task.catalog.value = { cases: [], endpoints: [{ path: '/health' }], warnings: [] }
  await task.submit()
  assert.deepEqual(submitted, ['http://127.0.0.1:8000/api/items?page=2', 'ws://127.0.0.1:9000/socket/events', 'http://127.0.0.1:8000/api/v2'])
  http.form.url = ''
  await http.send()
  socket.form.url = ''
  await socket.start()
  task.origin.value = ''
  await task.submit()
  assert.deepEqual(submitted.slice(3), ['http://127.0.0.1:8000/api', 'ws://127.0.0.1:9000/socket', 'http://127.0.0.1:8000/api'])
  assert.equal(prefixExports.joinTargetUrl('http://127.0.0.1:8000/api', '//other'), '')
  assert.equal(prefixExports.normalizeUrlPrefix('http://127.0.0.1:80/api/'), 'http://127.0.0.1:80/api')
})

test('protocol and host prefixes can be completed in the address field', () => {
  const { normalizeUrlPrefix, joinTargetUrl, urlSuffix } = prefixExports
  assert.equal(normalizeUrlPrefix('WS://'), 'ws://')
  assert.equal(joinTargetUrl('http://', '127.0.0.1:8002/api'), 'http://127.0.0.1:8002/api')
  assert.equal(joinTargetUrl('http://', ''), '')
  assert.equal(joinTargetUrl('http://', 'http://127.0.0.1'), '')
  assert.equal(joinTargetUrl('http://127.0.0.1', ':8002/api'), 'http://127.0.0.1:8002/api')
  assert.equal(joinTargetUrl('ws://[::1]', ':9000/events'), 'ws://[::1]:9000/events')
  assert.equal(joinTargetUrl('http://127.0.0.1:8002', ':9000/api'), '')
  assert.equal(joinTargetUrl('http://127.0.0.1', ':65536/api'), '')
  assert.equal(urlSuffix('http://127.0.0.1:8002/api', 'http://127.0.0.1'), ':8002/api')
  assert.equal(urlSuffix('127.0.0.1:8002/api', 'http://127.0.0.1', 'http://'), ':8002/api')
  assert.equal(urlSuffix(':8002/api', 'http://', 'http://127.0.0.1'), '127.0.0.1:8002/api')
})

test('case numbering starts again in each interface and permission skips are counted separately', async context => {
  const cases = ['/items', '/items?next=1', '/ask'].map(path => ({ path, name: path, protocol: 'http', method: 'POST', assertions: [] }))
  const task = { ...taskRecord('blocked', 'completed'), snapshot: { cases }, results: [
    { index: 0, status: 'permission_required' },
    { index: 2, status: 'skipped', reason: 'write_permission_required' },
  ] }
  const tree = load('TaskResultTree', [], { defineProps: () => ({ task }) }, 'components')
  context.after(tree.dispose)
  const output = await tree.render()
  assert.deepEqual([...output.matchAll(/<span class="muted">#(\d+)<\/span>/g)].map(match => match[1]), ['1', '2', '1'])
  assert.equal(resultExports.countCaseOutcomes(task.results).find(item => item.status === 'permission_required').count, 2)
  assert.match(output, /待授权/)
})

test('HTTPS task transport failures can fill the HTTP form without dispatching a new task', async context => {
  const page = load('TestCasesView', ['run', 'origin', 'tlsFailure', 'useHttpTarget'])
  context.after(page.dispose)
  page.run.value = { ...taskRecord('tls', 'completed'), snapshot: { origin: 'https://127.0.0.1:8002/api', cases: [] }, results: [
    { status: 'error', evidence: { result_code: 'TLS_ERROR' } },
  ] }
  assert.equal(page.tlsFailure.value, true)
  page.useHttpTarget()
  assert.equal(page.origin.value, 'http://127.0.0.1:8002/api')
  assert.equal(page.run.value.snapshot.origin, 'https://127.0.0.1:8002/api')
})

test('clearing a WebSocket timeline retains its live cursor and cancels stale replay display', async context => {
  const lateReplay = deferred()
  const live = { id: 'live', finished: false }
  const saved = { id: 'saved', finished: true }
  const page = load('LongConnection', ['clearTimeline', 'session', 'events', 'cursor', 'replay'], {
    readStream: () => lateReplay.promise,
  })
  context.after(page.dispose)
  page.session.value = live
  page.events.value = [{ sequence: 4 }]
  page.cursor.value = 4
  page.clearTimeline()
  assert.equal(page.session.value.id, 'live')
  assert.equal(page.cursor.value, 4)
  assert.deepEqual(page.events.value, [])
  page.session.value = saved
  const replay = page.replay(saved)
  page.clearTimeline()
  lateReplay.resolve({ session: saved, events: [{ sequence: 1 }], next_cursor: 1, has_more: false })
  await replay
  assert.equal(page.session.value, null)
  assert.deepEqual(page.events.value, [])
  await page.replay(saved)
  assert.equal(page.events.value[0].sequence, 1)
})

test('cleared task results stay hidden after polling and can be reopened from history', async context => {
  const pending = deferred()
  const completed = { ...taskRecord('saved', 'completed'), summary: '已保存总结' }
  let reads = 0
  const page = load('TestCasesView', ['openHistory', 'selectHistory', 'clearResults', 'run', 'running'], {
    readTask: () => ++reads === 1 ? pending.promise : Promise.resolve(completed),
    listTasks: async () => ({ items: [completed] }),
  })
  context.after(page.dispose)
  page.run.value = taskRecord('saved')
  const polling = page.openHistory('saved')
  page.clearResults()
  assert.equal(page.running.value, true)
  pending.resolve(completed)
  await polling
  assert.match(await page.render(), /已清除当前显示/)
  assert.doesNotMatch(await page.render(), /已保存总结/)
  await page.selectHistory('saved')
  assert.match(await page.render(), /已保存总结/)
})

test('task totals and only the active interface show a stream wait until evidence is recorded', async context => {
  const task = { ...taskRecord('streaming'), current_index: 1, active_stream: 'sse', snapshot: {
    cases: ['/health', '/events'].map(path => ({ path, name: path, protocol: 'http', method: 'GET', assertions: [] })),
  }, results: [{ index: 0, status: 'passed', assertions: [] }] }
  const page = load('TestCasesView', ['run', 'streamWait'])
  context.after(page.dispose)
  page.run.value = task
  const props = vue.reactive({ task, streamWait: page.streamWait.value })
  const tree = load('TaskResultTree', [], { defineProps: () => props }, 'components')
  context.after(tree.dispose)
  const overall = await page.render()
  assert.match(overall, /用例结果统计[\s\S]*等待 SSE 流结束[\s\S]*通过/)
  const operations = [...(await tree.render()).matchAll(/<summary class="operation-head">([\s\S]*?)<\/summary>/g)]
  assert.equal(operations.length, 2)
  assert.doesNotMatch(operations[0][1], /等待 SSE/)
  assert.match(operations[1][1], /等待 SSE 流结束/)
  page.run.value = { ...task, results: [...task.results, { index: 1, status: 'passed', assertions: [] }] }
  props.task = page.run.value
  props.streamWait = page.streamWait.value
  assert.doesNotMatch(await page.render(), /等待 SSE/)
  assert.doesNotMatch(await tree.render(), /等待 SSE/)
})

test('the active SSE case shows incremental data in its own result and retains the saved body', async context => {
  const task = { ...taskRecord('streaming'), current_index: 0, active_stream: 'sse', snapshot: {
    cases: ['/ask/stream', '/health'].map(path => ({ name: path, path, protocol: 'http', method: 'GET', assertions: [] })),
  } }
  const props = vue.reactive({ task, streamWait: { index: 0, label: '等待 SSE 流结束' }, liveEvents: [], liveEventsSeen: 0 })
  const tree = load('TaskResultTree', [], { defineProps: () => props }, 'components')
  context.after(tree.dispose)
  assert.match(await tree.render(), /测试结果[\s\S]*正在等待首条 SSE 事件/)
  props.liveEvents.push({ event: 'message', data: '第一段' })
  props.liveEventsSeen++
  assert.match(await tree.render(), /测试结果[\s\S]*data: 第一段/)
  props.liveEvents.push({ event: 'message', data: '第二段\n换行' })
  props.liveEventsSeen++
  assert.match(await tree.render(), /data: 第一段\n\ndata: 第二段\ndata: 换行/)
  props.task = { ...task, active_stream: null, status: 'completed', results: [{
    index: 0, status: 'passed', assertions: [], evidence: { response: {
      status: 200, headers: { 'x-detail-only': ['value'] }, sse: { events: [...props.liveEvents] },
    } },
  }] }
  props.streamWait = null
  props.liveEvents = []
  await vue.nextTick()
  const completed = await tree.render()
  assert.match(completed, /测试结果[\s\S]*data: 第一段\n\ndata: 第二段\ndata: 换行/)
  assert.doesNotMatch(completed, /x-detail-only|body_bytes/)
  assert.equal((completed.match(/aria-label="SSE 响应正文"/g) ?? []).length, 1)
})

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

test('settings defaults to enabled, validates timeout, and only sends generation_model when active or clearing', async context => {
  const payloads = []
  const toasts = []
  const page = load('SettingsView', ['form', 'loadSettings', 'saveSettings'], {
    getLLMConfig: async () => ({
      enabled: true,
      provider: 'openai',
      base_url: 'https://api.example/v1',
      model: 'gpt-4o',
      generation_model: null,
      timeout_seconds: 60,
      has_api_key: true,
    }),
    updateLLMConfig: async body => {
      payloads.push(JSON.parse(JSON.stringify(body)))
      return {
        enabled: body.enabled,
        provider: 'openai',
        base_url: body.base_url,
        model: body.model,
        generation_model: body.generation_model ?? null,
        timeout_seconds: body.timeout_seconds,
        has_api_key: true,
      }
    },
    useToastStore: () => ({ push: (...args) => toasts.push(args) }),
  })
  context.after(page.dispose)
  assert.equal(page.form.enabled, true)

  await page.loadSettings()
  page.form.timeoutSeconds = ''
  await page.saveSettings()
  assert.equal(payloads.length, 0)
  assert.match(toasts.at(-1)[1], /超时时间/)

  page.form.timeoutSeconds = 90
  await page.saveSettings()
  assert.equal('generation_model' in payloads[0], false)
  assert.equal(payloads[0].enabled, true)

  page.form.customGenerationModel = true
  page.form.generationModel = 'gen-model'
  await page.saveSettings()
  assert.equal(payloads[1].generation_model, 'gen-model')

  page.form.customGenerationModel = false
  await page.saveSettings()
  assert.equal(payloads[2].generation_model, null)
})

test('assistant composer sends on Enter and keeps newline for Shift+Enter or IME composition', async context => {
  const sent = []
  const assistant = load('TaskChat', ['input', 'onInputKeydown'], {
    defineProps: () => ({ scope: 'console' }),
    useId: () => 'chat-enter-test',
    chatWithAssistant: async (_taskId, messages) => {
      sent.push(messages.at(-1).content)
      return '收到'
    },
  }, 'components')
  context.after(assistant.dispose)

  assistant.input.value = '第一行'
  let prevented = false
  assistant.onInputKeydown({ key: 'Enter', shiftKey: true, isComposing: false, preventDefault: () => { prevented = true } })
  assert.equal(prevented, false)
  assert.deepEqual(sent, [])

  assistant.onInputKeydown({ key: 'Enter', shiftKey: false, isComposing: true, preventDefault: () => { prevented = true } })
  assert.equal(prevented, false)
  assert.deepEqual(sent, [])

  assistant.onInputKeydown({ key: 'Enter', shiftKey: false, isComposing: false, preventDefault: () => { prevented = true } })
  assert.equal(prevented, true)
  await Promise.resolve()
  assert.deepEqual(sent, ['第一行'])
})

test('assistant output auto-follows only when at bottom or empty before sending, and stops when scrolled up', async context => {
  let reply = deferred()
  let emitChunk = () => {}
  const makeScrollBox = (clientHeight, scrollHeight, scrollTop = 0) => {
    let top = scrollTop
    return {
      clientHeight,
      scrollHeight,
      get scrollTop() { return top },
      set scrollTop(value) { top = Math.max(0, Math.min(value, Math.max(0, this.scrollHeight - this.clientHeight))) },
    }
  }
  const assistant = load('TaskChat', ['input', 'turns', 'output', 'send', 'onOutputScroll'], {
    defineProps: () => ({ scope: 'console' }),
    useId: () => 'chat-anchor-test',
    chatWithAssistant: (_taskId, _messages, onText) => {
      emitChunk = onText
      return reply.promise
    },
  }, 'components')
  context.after(assistant.dispose)

  // 1. 没有输出时发送，后续每行流式输出自动跟进到底部
  const box = makeScrollBox(200, 200, 0)
  assistant.output.value = box
  assistant.input.value = '第一问'
  const first = assistant.send()
  box.scrollHeight = 360
  await vue.nextTick()
  assert.equal(box.scrollTop, 160)

  box.scrollHeight = 520
  emitChunk('第一行\n第二行')
  await vue.nextTick()
  assert.equal(box.scrollTop, 320)

  // 2. 流式输出中途用户向上滚离末行后，不再强制拉回底部
  box.scrollTop = 80
  assistant.onOutputScroll()
  box.scrollHeight = 700
  emitChunk('第一行\n第二行\n第三行')
  await vue.nextTick()
  assert.equal(box.scrollTop, 80)

  // 3. 用户滚回底部后重新建立锚点，继续自动跟进
  box.scrollTop = 500
  assistant.onOutputScroll()
  box.scrollHeight = 820
  reply.resolve('完整回复')
  await first
  await vue.nextTick()
  assert.equal(box.scrollTop, 620)

  // 4. 发送对话前未显示最后一行时，发送与输出均不自动跟进
  box.scrollTop = 40
  assistant.onOutputScroll()
  reply = deferred()
  assistant.input.value = '第二问'
  const second = assistant.send()
  box.scrollHeight = 980
  await vue.nextTick()
  assert.equal(box.scrollTop, 40)

  box.scrollHeight = 1120
  emitChunk('新的流式内容')
  await vue.nextTick()
  assert.equal(box.scrollTop, 40)

  reply.resolve('第二条回复')
  await second
  await vue.nextTick()
  assert.equal(box.scrollTop, 40)
})

test('deleting an active WebSocket session stops polling and resets live state', async context => {
  const pending = deferred()
  let pollSignal
  const page = load('LongConnection', ['start', 'removeHistory', 'session', 'events', 'history'], {
    startStream: async () => ({ id: 'ws-live', kind: 'websocket', status: 'open', finished: false }),
    readStream: (_id, _after, _wait, signal) => { pollSignal = signal; return pending.promise },
    listStreams: async () => ({ items: [{ id: 'ws-live', kind: 'websocket', url: 'ws://127.0.0.1:9100/ws', status: 'open', stop_reason: null, event_count: 1, created_at: '2026-11-22T00:00:00Z' }] }),
    deleteStream: async () => null,
  })
  context.after(page.dispose)

  await page.start()
  assert.equal(page.session.value.id, 'ws-live')
  await page.removeHistory({ id: 'ws-live' })
  assert.equal(pollSignal.aborted, true)
  assert.equal(page.session.value, null)
  assert.deepEqual(page.events.value, [])
  pending.resolve({ session: { id: 'ws-live', finished: true }, events: [{ sequence: 1 }], next_cursor: 1, has_more: false })
  await Promise.resolve()
  assert.equal(page.session.value, null)
})

test('history view confirms before clearing HTTP history and supports stream/task deletion with event preview', async context => {
  let cleared = 0
  const deleted = []
  let confirmed = false
  const storage = memoryStorage({
    'api-agent.chat-execution.console.h1': 'c1',
    'api-agent.chat.console.c1': '[]',
    'api-agent.chat-execution.websocket.s1': 'c2',
    'api-agent.chat.websocket.c2': '[]',
    'api-agent.chat-execution.tasks.t1': 'c3',
    'api-agent.chat.tasks.c3': '[]',
  })
  const page = load('HistoryView', [
    'tab', 'httpItems', 'streamItems', 'taskItems', 'selectedStream', 'streamEvents',
    'selectedTaskId', 'task', 'selectStream', 'removeStreamRecord', 'removeTaskRecord', 'clearAll',
  ], {
    storage,
    window: {
      localStorage: storage,
      matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
      confirm: () => confirmed,
      addEventListener: () => {},
      setTimeout: () => 1,
      clearTimeout: () => {},
      setInterval: () => 1,
      clearInterval: () => {},
    },
    clearConsoleHistory: async () => { cleared++ },
    deleteStream: async id => { deleted.push(['stream', id]) },
    deleteTask: async id => { deleted.push(['task', id]) },
    readStream: async () => ({
      session: { id: 's1', finished: true },
      events: [{ sequence: 1, direction: 'in', kind: 'text', data: 'welcome-1', elapsed_ms: 5 }],
      next_cursor: 1,
      has_more: false,
    }),
    describeStopReason: () => '正常关闭',
    formatBytes: () => '10 B',
    prettyJson: value => value,
  })
  context.after(page.dispose)

  page.httpItems.value = [{ id: 'h1' }]
  await page.clearAll()
  assert.equal(cleared, 0)
  assert.equal(page.httpItems.value.length, 1)

  confirmed = true
  await page.clearAll()
  assert.equal(cleared, 1)
  assert.equal(page.httpItems.value.length, 0)
  assert.equal(storage.getItem('api-agent.chat.console.c1'), null)

  page.streamItems.value = [{ id: 's1', kind: 'websocket', url: 'ws://127.0.0.1:9100/ws', status: 'closed', event_count: 1, byte_count: 9, created_at: '2026-11-22T00:00:00Z' }]
  await page.selectStream(page.streamItems.value[0])
  assert.equal(page.streamEvents.value.length, 1)
  await page.removeStreamRecord(page.streamItems.value[0])
  assert.equal(page.selectedStream.value, null)
  assert.equal(storage.getItem('api-agent.chat.websocket.c2'), null)

  page.taskItems.value = [{ id: 't1', status: 'completed', created_at: '2026-11-22T00:00:00Z' }]
  page.selectedTaskId.value = 't1'
  page.task.value = { id: 't1', status: 'completed', snapshot: { cases: [] }, results: [], summary: '## 结论' }
  await page.removeTaskRecord('t1')
  assert.equal(page.task.value, null)
  assert.equal(storage.getItem('api-agent.chat.tasks.c3'), null)
  assert.deepEqual(deleted, [['stream', 's1'], ['task', 't1']])
})

test('history view filters records by URL/keyword and status across tabs and resets status on tab switch', async context => {
  const page = load('HistoryView', [
    'tab', 'searchQuery', 'statusFilter', 'httpItems', 'streamItems', 'taskItems',
    'filteredHttpItems', 'filteredStreamItems', 'filteredTaskItems', 'resetFilters',
  ], {
    window: {
      localStorage: memoryStorage(),
      matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
      confirm: () => true,
      addEventListener: () => {},
      setTimeout: () => 1,
      clearTimeout: () => {},
      setInterval: () => 1,
      clearInterval: () => {},
    },
    describeStopReason: () => '',
    formatBytes: () => '0 B',
    prettyJson: value => value,
  })
  context.after(page.dispose)

  page.httpItems.value = [
    { id: 'h1', method: 'GET', url: 'http://127.0.0.1:8010/health', ok: true, status_code: 200, response_headers: {} },
    { id: 'h2', method: 'POST', url: 'http://127.0.0.1:8010/api/login', ok: true, status_code: 401, response_headers: {} },
    { id: 'h3', method: 'GET', url: 'http://127.0.0.1:9100/sse', ok: true, status_code: 200, response_headers: { 'content-type': ['text/event-stream'] } },
    { id: 'h4', method: 'GET', url: 'http://127.0.0.1:9100/down', ok: false, status_code: null, error_kind: 'request_timeout', response_headers: {} },
  ]
  page.streamItems.value = [
    { id: 's1', kind: 'websocket', url: 'ws://127.0.0.1:9100/ws', status: 'closed', stop_reason: 'client_stopped' },
    { id: 's2', kind: 'websocket', url: 'ws://127.0.0.1:9100/reject', status: 'error', stop_reason: 'handshake_rejected' },
    { id: 's3', kind: 'websocket', url: 'ws://127.0.0.1:9100/live', status: 'connecting', stop_reason: null },
  ]
  page.taskItems.value = [
    { id: 't1', title: 'CRM 冒烟', origin: 'http://127.0.0.1:8088', status: 'completed' },
    { id: 't2', title: '订单测试', origin: 'http://127.0.0.1:9100', status: 'partial' },
    { id: 't3', title: '超时回归', origin: 'http://127.0.0.1:9100', status: 'timed_out' },
  ]

  page.statusFilter.value = '4xx'
  assert.deepEqual(page.filteredHttpItems.value.map(item => item.id), ['h2'])
  page.statusFilter.value = 'all'
  page.searchQuery.value = '9100'
  assert.deepEqual(page.filteredHttpItems.value.map(item => item.id), ['h3', 'h4'])
  page.statusFilter.value = 'error'
  assert.deepEqual(page.filteredHttpItems.value.map(item => item.id), ['h4'])
  page.statusFilter.value = 'sse'
  assert.deepEqual(page.filteredHttpItems.value.map(item => item.id), ['h3'])

  page.tab.value = 'stream'
  await vue.nextTick()
  assert.equal(page.statusFilter.value, 'all')
  assert.deepEqual(page.filteredStreamItems.value.map(item => item.id), ['s1', 's2', 's3'])
  page.statusFilter.value = 'open'
  assert.deepEqual(page.filteredStreamItems.value.map(item => item.id), ['s3'])
  page.statusFilter.value = 'error'
  assert.deepEqual(page.filteredStreamItems.value.map(item => item.id), ['s2'])

  page.tab.value = 'task'
  await vue.nextTick()
  assert.equal(page.statusFilter.value, 'all')
  assert.deepEqual(page.filteredTaskItems.value.map(item => item.id), ['t2', 't3'])
  page.statusFilter.value = 'partial'
  assert.deepEqual(page.filteredTaskItems.value.map(item => item.id), ['t2'])
  page.statusFilter.value = 'timed_out'
  assert.deepEqual(page.filteredTaskItems.value.map(item => item.id), ['t3'])
  page.resetFilters()
  assert.deepEqual(page.filteredTaskItems.value.map(item => item.id), ['t1', 't2', 't3'])
})

test('WebSocket headers and subprotocols start collapsed and timeline events toggle independently', async context => {
  const started = []
  const page = load('LongConnection', [
    'SAMPLES', 'applySample', 'form', 'urlPrefix', 'session', 'events', 'toggleEvent', 'toggleAllEvents', 'isEventCollapsed',
  ], {
    startStream: async input => { started.push(input); return { id: 'ws-started', finished: true } },
    formatBytes: value => `${value ?? 0} B`,
    describeStopReason: () => '',
    looksLikeJson: value => value.trim().startsWith('{'),
    prettyJson: value => value,
  })
  context.after(page.dispose)

  page.urlPrefix.value = 'ws://127.0.0.1:9000'
  page.applySample(page.SAMPLES[0])
  assert.equal(page.SAMPLES[0].label, 'CRM 实时对话')
  assert.equal(page.urlPrefix.value, '')
  assert.equal(page.form.url, 'ws://127.0.0.1:8088/api/ws/chat?room=lobby&name=API测试端')
  assert.equal(page.form.scheduled, '你好，我是 API-Agent！')
  assert.deepEqual(started, [])

  page.session.value = { id: 'ws-ui', kind: 'websocket', status: 'open', finished: false, url: 'ws://127.0.0.1:8088/api/ws/chat', event_count: 3, sent_count: 1, byte_count: 64 }
  page.events.value = [
    { sequence: 1, direction: 'in', kind: 'open', data: 'WebSocket handshake complete', data_bytes: 28, truncated: false, elapsed_ms: 12 },
    { sequence: 2, direction: 'out', kind: 'text', data: '{"type":"ping"}', data_bytes: 15, truncated: false, elapsed_ms: 35 },
    { sequence: 3, direction: 'in', kind: 'text', data: '{"type":"pong","onlineCount":2}', data_bytes: 31, truncated: false, elapsed_ms: 48 },
  ]

  const initialHtml = await page.render()
  assert.match(initialHtml, /class="sample-chip"[^>]*>CRM 实时对话<\/button>/)
  assert.match(initialHtml, /<details\b(?![^>]*\sopen(?:\s|=|>))[^>]*>[\s\S]*?<summary[^>]*>[\s\S]*?请求头[\s\S]*?<\/summary>/)
  assert.match(initialHtml, /<details\b(?![^>]*\sopen(?:\s|=|>))[^>]*>[\s\S]*?<summary[^>]*>[\s\S]*?子协议（逗号分隔）[\s\S]*?<\/summary>/)
  assert.match(initialHtml, /event-tone-open/)
  assert.match(initialHtml, /event-tone-out/)
  assert.match(initialHtml, /event-tone-in/)
  assert.match(initialHtml, /class="event-body"/)

  // 折叠第 2 条发送记录，第 3 条接收记录仍保持展开
  page.toggleEvent(2)
  assert.equal(page.isEventCollapsed(2), true)
  assert.equal(page.isEventCollapsed(3), false)
  const foldedOne = await page.render()
  assert.match(foldedOne, /event-collapsed/)
  assert.match(foldedOne, /class="event-preview"/)
  assert.match(foldedOne, /class="event-body"[^>]*>[^<]*pong/)

  // 批量折叠与批量展开
  page.toggleAllEvents()
  assert.equal(page.isEventCollapsed(1), true)
  assert.equal(page.isEventCollapsed(2), true)
  assert.equal(page.isEventCollapsed(3), true)
  assert.doesNotMatch(await page.render(), /class="event-body"/)

  page.toggleAllEvents()
  assert.equal(page.isEventCollapsed(1), false)
  assert.equal(page.isEventCollapsed(2), false)
  assert.equal(page.isEventCollapsed(3), false)
  assert.match(await page.render(), /class="event-body"/)
})

test('unified UrlInput renders quick protocol prefixes and powers ModelTestingView and SettingsView without model test history', async context => {
  const discovered = []
  const probed = []
  const savedSettings = []

  const urlProps = {
    modelValue: 'https://',
    address: 'provider.example.com/v1',
    label: 'Base URL',
    placeholder: 'https://api.openai.com/v1',
    websocket: false,
    disabled: false,
  }
  const urlInput = load('UrlInput', [], {
    ...urlProps,
    defineProps: () => urlProps,
  }, 'components')
  context.after(urlInput.dispose)
  const urlHtml = await urlInput.render()
  assert.match(urlHtml, /常用协议/)
  assert.match(urlHtml, /https:\/\//)
  assert.match(urlHtml, /http:\/\//)
  assert.doesNotMatch(urlHtml, /ws:\/\//)

  const wsUrlProps = { ...urlProps, modelValue: '', websocket: true }
  const wsUrlInput = load('UrlInput', [], {
    ...wsUrlProps,
    defineProps: () => wsUrlProps,
  }, 'components')
  context.after(wsUrlInput.dispose)
  const wsUrlHtml = await wsUrlInput.render()
  assert.match(wsUrlHtml, /ws:\/\//)
  assert.match(wsUrlHtml, /wss:\/\//)

  const modelPage = load('ModelTestingView', [
    'form', 'urlPrefix', 'selectUrlPrefix', 'effectiveBaseUrl', 'discover', 'runProbe',
  ], {
    getLLMConfig: async () => ({
      enabled: true,
      provider: 'openai',
      base_url: 'https://provider.example.com/v1',
      model: 'deepseek-v4.1-flash',
      generation_model: null,
      timeout_seconds: 60,
      has_api_key: true,
    }),
    discoverProviderModels: async payload => {
      discovered.push(payload)
      return {
        ok: true,
        base_url: 'https://provider.example.com/v1',
        http_status: 200,
        latency_ms: 120,
        count: 1,
        message: '成功拉取 1 个模型',
        models: [{
          id: 'deepseek-v4.1-flash',
          owned_by: null,
          created: null,
          model_type: 'chat',
          type_source: 'inferred',
          availability: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
          tool_calling: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
          input_modalities: [],
          output_modalities: [],
          raw_metadata: {},
        }],
      }
    },
    probeProviderModels: async payload => {
      probed.push(payload)
      return { ok: true, base_url: payload.base_url, tested_count: 1, available_count: 1, tool_supported_count: 1, message: 'done', results: [] }
    },
    useToastStore: () => ({ push: () => {} }),
  })
  context.after(modelPage.dispose)

  modelPage.selectUrlPrefix('https://')
  modelPage.form.baseUrl = 'provider.example.com'
  assert.equal(modelPage.effectiveBaseUrl.value, 'https://provider.example.com')
  await modelPage.discover()
  assert.equal(discovered[0].base_url, 'https://provider.example.com')
  assert.equal(modelPage.urlPrefix.value, 'https://')
  assert.equal(modelPage.form.baseUrl, 'provider.example.com/v1')
  assert.equal(modelPage.effectiveBaseUrl.value, 'https://provider.example.com/v1')
  await modelPage.runProbe(['deepseek-v4.1-flash'])
  assert.equal(probed[0].base_url, 'https://provider.example.com/v1')

  const modelHtml = await modelPage.render()
  assert.match(modelHtml, /<h1>大模型测试<\/h1>/)
  assert.doesNotMatch(modelHtml, /模型测试服务/)
  assert.doesNotMatch(modelHtml, /测试历史/)

  const settingsPage = load('SettingsView', ['form', 'urlPrefix', 'selectUrlPrefix', 'saveSettings'], {
    updateLLMConfig: async body => {
      savedSettings.push(body)
      return {
        enabled: true,
        provider: 'openai',
        base_url: body.base_url,
        model: body.model,
        generation_model: null,
        timeout_seconds: body.timeout_seconds,
        has_api_key: false,
      }
    },
    useToastStore: () => ({ push: () => {} }),
  })
  context.after(settingsPage.dispose)
  settingsPage.selectUrlPrefix('https://')
  settingsPage.form.baseUrl = 'api.example.com/v1'
  await settingsPage.saveSettings()
  assert.equal(savedSettings[0].base_url, 'https://api.example.com/v1')
})

test('WebSocket composer sits above headers with bidirectional send and TestCases settings uses matching bordered panel', async context => {
  const sentMessages = []
  const socket = load('LongConnection', [
    'form', 'session', 'outgoing', 'messageDraft', 'sendMessage',
  ], {
    sendStreamMessage: async (id, message) => {
      sentMessages.push({ id, message })
      return { id, kind: 'websocket', status: 'open', finished: false, url: 'ws://127.0.0.1:8081/api/ws/chat', event_count: 2, sent_count: 1, byte_count: 32 }
    },
    formatBytes: value => `${value ?? 0} B`,
    describeStopReason: () => '',
    looksLikeJson: value => value.trim().startsWith('{'),
    prettyJson: value => value,
  })
  context.after(socket.dispose)

  // 未连接时编辑自动发送内容，且输入区位于请求头之前
  socket.messageDraft.value = '{"type":"subscribe"}'
  assert.equal(socket.form.scheduled, '{"type":"subscribe"}')
  const idleHtml = await socket.render()
  assert.ok(idleHtml.indexOf('message-composer') > 0)
  assert.ok(idleHtml.indexOf('message-composer') < idleHtml.indexOf('请求头'))
  assert.match(idleHtml, /composer-send-btn[^>]*disabled/)

  // 建立连接后自动切换为双向实时发送模式并调用 sendStreamMessage
  socket.session.value = { id: 'ws-live', kind: 'websocket', status: 'open', finished: false, url: 'ws://127.0.0.1:8081/api/ws/chat', event_count: 1, sent_count: 0, byte_count: 16 }
  assert.equal(socket.messageDraft.value, '')
  socket.messageDraft.value = '你好，实时测试'
  assert.equal(socket.outgoing.value, '你好，实时测试')
  assert.equal(socket.form.scheduled, '{"type":"subscribe"}')
  const liveHtml = await socket.render()
  assert.match(liveHtml, /composer-mode-live/)
  assert.doesNotMatch(liveHtml, /class="send-bar"/)
  await socket.sendMessage()
  assert.deepEqual(sentMessages, [{ id: 'ws-live', message: '你好，实时测试' }])
  assert.equal(socket.outgoing.value, '')

  // 智能群测设置折叠块具备与长连接设置一致的边框样式
  const taskCss = fs.readFileSync(path.join(__dirname, '../src/views/TestCasesView.vue'), 'utf8')
  const settingsRule = taskCss.slice(taskCss.indexOf('.settings {'), taskCss.indexOf('.settings:hover'))
  assert.match(settingsRule, /border:\s*1px solid var\(--border-subtle\)/)
  assert.match(settingsRule, /border-radius:\s*var\(--radius-md\)/)
  assert.match(settingsRule, /background:\s*var\(--bg-subtle\)/)
})

test('ModelTestingView splits input and results, supports multi-filters, adjacent system buttons, and modal-confirmed clear all', async context => {
  const sampleModels = [
    {
      id: 'chat-pro-vision',
      owned_by: 'acme',
      created: null,
      model_type: 'chat',
      type_source: 'inferred',
      availability: { status: 'available', source: 'probed', latency_ms: 1200, detail: 'ok' },
      tool_calling: { status: 'supported', source: 'probed', latency_ms: 900, detail: 'tool_calls' },
      input_modalities: [
        { modality: 'text', status: 'supported', source: 'probed', detail: null },
        { modality: 'image', status: 'supported', source: 'probed', detail: null },
      ],
      output_modalities: [
        { modality: 'text', status: 'supported', source: 'probed', detail: null },
        { modality: 'reasoning', status: 'supported', source: 'probed', detail: null },
      ],
      raw_metadata: {},
    },
    {
      id: 'chat-text-only',
      owned_by: 'acme',
      created: null,
      model_type: 'chat',
      type_source: 'inferred',
      availability: { status: 'unavailable', source: 'probed', latency_ms: 500, detail: 'HTTP 502' },
      tool_calling: { status: 'unsupported', source: 'probed', latency_ms: 300, detail: '400' },
      input_modalities: [
        { modality: 'text', status: 'inferred', source: 'inferred', detail: null },
        { modality: 'image', status: 'unsupported', source: 'probed', detail: null },
      ],
      output_modalities: [
        { modality: 'text', status: 'inferred', source: 'inferred', detail: null },
      ],
      raw_metadata: {},
    },
    {
      id: 'flux-image-gen',
      owned_by: 'studio',
      created: null,
      model_type: 'image',
      type_source: 'inferred',
      availability: { status: 'unsupported', source: 'untested', latency_ms: null, detail: null },
      tool_calling: { status: 'unsupported', source: 'inferred', latency_ms: null, detail: null },
      input_modalities: [{ modality: 'text', status: 'inferred', source: 'inferred', detail: null }],
      output_modalities: [{ modality: 'image', status: 'inferred', source: 'inferred', detail: null }],
      raw_metadata: {},
    },
  ]

  const page = load('ModelTestingView', [
    'form', 'urlPrefix', 'checks', 'savedConfig', 'discoverMeta', 'models', 'selectedIds', 'activeModelId',
    'filteredModels', 'categoryFilter', 'availabilityFilter', 'toolFilter', 'inputModalityFilter',
    'outputModalityFilter', 'searchQuery', 'clearConfirmOpen', 'loadSavedDefaults', 'fillFromSystemSettings',
    'toggleUseSavedKey', 'requestClearAll', 'cancelClearAll', 'confirmClearAll', 'resetFilters',
  ], {
    getLLMConfig: async () => ({
      enabled: true,
      provider: 'openai',
      base_url: 'https://provider.example.com/v1',
      model: 'chat-pro-vision',
      generation_model: null,
      timeout_seconds: 60,
      has_api_key: true,
    }),
    useToastStore: () => ({ push: () => {} }),
  })
  context.after(page.dispose)

  await page.loadSavedDefaults()
  page.fillFromSystemSettings()
  assert.equal(page.form.baseUrl, 'https://provider.example.com/v1')
  assert.equal(page.form.useSavedKey, true)
  assert.equal(page.form.timeoutSeconds, 60)
  page.toggleUseSavedKey()
  assert.equal(page.form.useSavedKey, false)

  // 注入模拟响应结果并验证多维筛选
  page.urlPrefix.value = 'https://'
  page.form.baseUrl = 'provider.example.com/v1'
  page.form.apiKey = 'ephemeral-test-key'
  page.form.timeoutSeconds = 45
  page.checks.output_modalities = true
  page.discoverMeta.value = { ok: true, base_url: 'https://provider.example.com/v1', http_status: 200, latency_ms: 210, count: 3, message: 'ok' }
  page.models.value = sampleModels
  page.selectedIds.value = ['chat-pro-vision']
  page.activeModelId.value = 'chat-pro-vision'

  page.availabilityFilter.value = 'available'
  assert.deepEqual(page.filteredModels.value.map(item => item.id), ['chat-pro-vision'])

  page.resetFilters()
  page.toolFilter.value = 'supported'
  assert.deepEqual(page.filteredModels.value.map(item => item.id), ['chat-pro-vision'])

  page.resetFilters()
  page.inputModalityFilter.value = 'image_probed'
  assert.deepEqual(page.filteredModels.value.map(item => item.id), ['chat-pro-vision'])

  page.resetFilters()
  page.inputModalityFilter.value = 'text_only'
  assert.deepEqual(page.filteredModels.value.map(item => item.id), ['chat-text-only', 'flux-image-gen'])

  page.resetFilters()
  page.outputModalityFilter.value = 'reasoning'
  assert.deepEqual(page.filteredModels.value.map(item => item.id), ['chat-pro-vision'])

  page.resetFilters()
  page.outputModalityFilter.value = 'image'
  assert.deepEqual(page.filteredModels.value.map(item => item.id), ['flux-image-gen'])

  // 渲染结构校验：左右分栏 + 系统设置双按钮紧挨 + 清空确认弹窗文案
  const sfc = fs.readFileSync(path.join(__dirname, '../src/views/ModelTestingView.vue'), 'utf8')
  assert.match(sfc, /<TestWorkspace[\s\S]*editor-key="api-agent\.layout\.model-testing\.config"/)
  assert.match(sfc, /<AppModal[\s\S]*:open="clearConfirmOpen"/)
  const systemActionsRule = sfc.slice(sfc.indexOf('.system-actions {'), sfc.indexOf('.input-form {'))
  assert.match(systemActionsRule, /display:\s*grid/)
  assert.match(systemActionsRule, /grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/)
  assert.doesNotMatch(systemActionsRule, /flex-wrap:\s*wrap/)
  assert.doesNotMatch(systemActionsRule, /font-weight:\s*600/)
  const filterSelectRule = sfc.slice(sfc.indexOf('.filter-select {'), sfc.indexOf('.filter-count {'))
  assert.match(filterSelectRule, /width:\s*auto/)
  assert.match(filterSelectRule, /flex:\s*0 0 auto/)
  const tableWrapRule = sfc.slice(sfc.indexOf('.table-wrap {'), sfc.indexOf('.model-table tbody tr {'))
  assert.match(tableWrapRule, /overflow-y:\s*auto/)
  assert.match(tableWrapRule, /max-height:\s*clamp\(/)
  assert.match(tableWrapRule, /position:\s*sticky/)
  const html = await page.render()
  assert.match(html, /class="input-pane scroll-y"/)
  assert.match(html, /class="result-pane scroll-y"/)
  assert.match(html, /class="system-actions"[\s\S]*填入系统设置地址[\s\S]*复用系统同源密钥/)
  assert.match(html, /确认清空/)

  // 清空弹窗：取消保留所有内容，确认则全部输入输出清空
  page.requestClearAll()
  assert.equal(page.clearConfirmOpen.value, true)
  page.cancelClearAll()
  assert.equal(page.clearConfirmOpen.value, false)
  assert.equal(page.models.value.length, 3)
  assert.equal(page.form.apiKey, 'ephemeral-test-key')

  page.requestClearAll()
  page.confirmClearAll()
  assert.equal(page.clearConfirmOpen.value, false)
  assert.equal(page.urlPrefix.value, '')
  assert.equal(page.form.baseUrl, '')
  assert.equal(page.form.apiKey, '')
  assert.equal(page.form.useSavedKey, false)
  assert.equal(page.form.protocol, 'auto')
  assert.equal(page.form.timeoutSeconds, 25)
  assert.equal(page.checks.output_modalities, false)
  assert.equal(page.checks.streaming, false)
  assert.equal(page.checks.structured_output, false)
  assert.equal(page.discoverMeta.value, null)
  assert.deepEqual(page.models.value, [])
  assert.deepEqual(page.selectedIds.value, [])
  assert.equal(page.activeModelId.value, null)
  assert.equal(page.availabilityFilter.value, 'all')
  assert.equal(page.toolFilter.value, 'all')
  assert.equal(page.inputModalityFilter.value, 'all')
  assert.equal(page.outputModalityFilter.value, 'all')
  assert.equal(page.searchQuery.value, '')
})

test('ModelTestingView supports protocol selection, streaming TTFT, and structured output badges', async context => {
  const discoverCalls = []
  const probeCalls = []
  const page = load('ModelTestingView', [
    'form', 'checks', 'models', 'selectedIds', 'discoverMeta', 'discover', 'runProbe',
  ], {
    getLLMConfig: async () => null,
    discoverProviderModels: async body => {
      discoverCalls.push(body)
      return {
        ok: true,
        protocol: 'anthropic',
        base_url: 'https://api.anthropic.com/v1',
        http_status: 200,
        latency_ms: 95,
        count: 1,
        message: '成功拉取 1 个模型',
        models: [
          {
            id: 'claude-sonnet-4-5',
            owned_by: 'anthropic',
            created: null,
            model_type: 'chat',
            type_source: 'inferred',
            availability: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
            tool_calling: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
            streaming: { status: 'untested', source: 'untested', latency_ms: null, ttft_ms: null, detail: null },
            structured_output: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
            input_modalities: [{ modality: 'text', status: 'inferred', source: 'inferred', detail: null }],
            output_modalities: [{ modality: 'text', status: 'inferred', source: 'inferred', detail: null }],
            raw_metadata: {},
          },
        ],
      }
    },
    probeProviderModels: async body => {
      probeCalls.push(body)
      return {
        ok: true,
        protocol: 'anthropic',
        base_url: 'https://api.anthropic.com/v1',
        tested_count: 1,
        available_count: 1,
        tool_supported_count: 1,
        streaming_supported_count: 1,
        structured_output_supported_count: 1,
        message: '已完成探测',
        results: [
          {
            id: 'claude-sonnet-4-5',
            owned_by: 'anthropic',
            created: null,
            model_type: 'chat',
            type_source: 'inferred',
            availability: { status: 'available', source: 'probed', latency_ms: 420, detail: 'Anthropic Messages 可用' },
            tool_calling: { status: 'supported', source: 'probed', latency_ms: 380, detail: 'tool_use' },
            streaming: { status: 'supported', source: 'probed', latency_ms: 510, ttft_ms: 145, detail: 'SSE 可用' },
            structured_output: { status: 'supported', source: 'probed', latency_ms: 410, detail: 'Schema 支持' },
            input_modalities: [{ modality: 'text', status: 'supported', source: 'probed', detail: null }],
            output_modalities: [
              { modality: 'text', status: 'supported', source: 'probed', detail: null },
              { modality: 'json', status: 'supported', source: 'probed', detail: null },
            ],
            raw_metadata: {},
          },
        ],
      }
    },
    useToastStore: () => ({ push: () => {} }),
  })
  context.after(page.dispose)

  page.form.baseUrl = 'https://api.anthropic.com/v1'
  page.form.protocol = 'anthropic'
  page.checks.streaming = true
  page.checks.structured_output = true
  await page.discover()
  assert.equal(discoverCalls[0].protocol, 'anthropic')
  assert.equal(page.discoverMeta.value.protocol, 'anthropic')

  await page.runProbe(['claude-sonnet-4-5'])
  assert.equal(probeCalls[0].protocol, 'anthropic')
  assert.ok(probeCalls[0].checks.includes('streaming'))
  assert.ok(probeCalls[0].checks.includes('structured_output'))

  const html = await page.render()
  assert.match(html, /TTFT 145ms/)
  assert.match(html, /Schema/)
  assert.match(html, /Anthropic/)
})

test('TaskDraft accepts bound variable placeholders and extract rules while TaskResultTree renders extracted values', async context => {
  const emitted = []
  const draft = load('TaskDraft', ['beginEdit', 'saveEdit', 'path', 'extract', 'error'], {
    defineProps: () => ({
      editing: false,
      catalog: {
        format: 'openapi',
        endpoints: [
          {
            name: '详情',
            protocol: 'http',
            method: 'GET',
            path: '/api/customer/1',
            path_template: '/api/customer/{id}',
            source: 'document',
            assertions: [{ kind: 'status', expected: 200 }],
          },
        ],
        cases: [],
        warnings: [],
      },
    }),
    defineEmits: () => (event, ...args) => emitted.push([event, ...args]),
  }, 'components')
  context.after(draft.dispose)

  draft.beginEdit(
    'op',
    0,
    '/api/customer/{id}',
    { kind: 'example', operation: 0 },
    {
      name: '获取客户详情',
      protocol: 'http',
      method: 'GET',
      path: '/api/customer/1',
      source: 'document',
      headers: { token: '{{crm_token}}' },
      assertions: [{ kind: 'status', expected: 200 }],
    },
  )
  draft.path.value = '/api/customer/{{customer_id}}'
  draft.extract.value = JSON.stringify([{ name: 'customer_name', source: 'json', pointer: '/data/name' }])
  draft.saveEdit()
  assert.equal(draft.error.value, '')
  const changeCall = emitted.find(([evt]) => evt === 'change')
  assert.ok(changeCall)
  assert.equal(changeCall[2].path, '/api/customer/{{customer_id}}')
  assert.equal(changeCall[2].extract[0].name, 'customer_name')

  assert.match(
    taskResultExports.taskCaseMessage({ index: 0, name: 'x', protocol: 'http', source: 'document', status: 'skipped', reason: 'missing_variable:crm_token', assertions: [] }),
    /crm_token/,
  )
})

test('RequestConsole scope notice reflects unrestricted valid targets with optional allowlist guardrails', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'src/views/RequestConsole.vue'), 'utf8')
  assert.doesNotMatch(source, /只允许数字回环地址/)
  assert.doesNotMatch(source, /target_not_loopback/)
  assert.match(source, /TARGET_ALLOWED_ORIGINS/)
  assert.match(source, /跨源重定向自动剥离认证头/)
})

test('AssistantAvatar renders green robot expressions and randomly switches expressions on timer', async context => {
  const avatar = load('AssistantAvatar', ['expression', 'nextRandomExpression', 'EXPRESSIONS'], {
    defineProps: () => ({ size: 24, state: 'idle', minIntervalMs: 1000, maxIntervalMs: 1000 }),
  }, 'components')
  context.after(avatar.dispose)

  assert.ok(avatar.EXPRESSIONS.length >= 8)
  const initial = avatar.expression.value
  const switched = avatar.nextRandomExpression()
  assert.notEqual(switched, initial)
  assert.equal(avatar.expression.value, switched)

  const html = await avatar.render()
  assert.match(html, /class="assistant-avatar/)
  assert.match(html, new RegExp(`data-expression="${switched}"`))

  const avatarSource = fs.readFileSync(path.join(__dirname, '..', 'src/components/AssistantAvatar.vue'), 'utf8')
  assert.match(avatarSource, /var\(--assistant-color,\s*#22c55e\)/)
})

test('TaskChat displays animated thinking/reading ellipsis cycle and forwards current test context', async context => {
  const reply = deferred()
  let capturedOptions = null
  const currentTest = assistantContextExports.buildConsoleTestContext({
    method: 'POST',
    url: 'http://127.0.0.1:8088/api/customer',
    headers: { Authorization: 'Bearer super-secret', 'X-Trace': 'trace-42' },
    bodyMode: 'json',
    body: '{"name":"demo"}',
    result: {
      ok: true,
      request: { method: 'POST', url: 'http://127.0.0.1:8088/api/customer' },
      response: { status: 500, reason: 'Error', headers: {}, body: '{"err":"boom"}', body_bytes: 14, body_truncated: false, body_encoding: null, elapsed_ms: 12, redirects: [] },
      error_kind: null,
      error_message: null,
      elapsed_ms: 12,
    },
    liveEvents: null,
    localFailure: null,
    responseCleared: false,
  })
  assert.equal(currentTest.request.headers.Authorization, '[redacted]')
  assert.equal(currentTest.request.headers['X-Trace'], 'trace-42')

  const assistant = load('TaskChat', ['input', 'send', 'statusPhase', 'statusLabel', 'statusDots', 'dotCount'], {
    defineProps: () => ({ scope: 'console', currentTest }),
    useId: () => 'chat-status-test',
    chatWithAssistant: (_taskId, _messages, onText, options) => {
      capturedOptions = { onText, options }
      return reply.promise
    },
  }, 'components')
  context.after(assistant.dispose)

  assistant.input.value = '为什么报 500？'
  const pending = assistant.send()
  assert.equal(assistant.statusPhase.value, 'thinking')
  assert.equal(assistant.statusLabel.value, '努力思考中')
  assert.equal(assistant.statusDots.value, '.')
  assert.match(await assistant.render(), /努力思考中<\/span><span[^>]*>\.<\/span>/)

  // 模拟省略号循环动画：. -> .. -> ... -> .
  assistant.tick()
  assert.equal(assistant.statusDots.value, '..')
  assistant.tick()
  assert.equal(assistant.statusDots.value, '...')
  assistant.tick()
  assert.equal(assistant.statusDots.value, '.')

  // 触发读取当前测试数据工具状态
  capturedOptions.options.onStatus({ phase: 'reading_data', tool: 'read_current_test_result', message: '读取数据中' })
  assert.equal(assistant.statusPhase.value, 'reading_data')
  assert.equal(assistant.statusLabel.value, '读取数据中')
  assistant.tick()
  assert.match(await assistant.render(), /读取数据中<\/span><span[^>]*>\.\.<\/span>/)
  assert.equal(capturedOptions.options.currentTest.response.status, 500)

  // 模型开始流式输出后展示正文并结束等待态
  capturedOptions.onText('根因：服务端返回 500')
  assert.match(await assistant.render(), /根因：服务端返回 500/)
  reply.resolve('根因：服务端返回 500，建议检查必填字段。')
  await pending
  assert.equal(assistant.statusPhase.value, null)
})

test('KnowledgeBaseSettings loads prepared corpus, saves embedding config and triggers vector build', async context => {
  const savedPayloads = []
  let buildCount = 0
  const baseStatus = {
    status: 'unconfigured',
    configured: false,
    ready: false,
    retrieval_mode: 'lexical_fallback',
    corpus_hash: 'abc123',
    chunk_count: 20,
    vector_count: 0,
    embedding_model: null,
    embedding_dimensions: null,
    built_at: null,
    last_error: null,
    config: {
      enabled: true,
      use_llm_endpoint: true,
      base_url: null,
      effective_base_url: 'http://127.0.0.1:11434/v1',
      model: '',
      dimensions: null,
      top_k: 4,
      timeout_seconds: 30,
      has_api_key: false,
      has_effective_api_key: false,
    },
    documents: [{ doc_id: 'overview', category: '项目总览', title: '总览', sections: ['页面'], chunk_count: 2 }],
  }
  const kb = load('KnowledgeBaseSettings', ['loadStatus', 'form', 'saveConfig', 'buildVectors', 'status'], {
    getKnowledgeStatus: async () => buildCount ? { ...baseStatus, status: 'ready', configured: true, ready: true, retrieval_mode: 'vector', vector_count: 20, embedding_dimensions: 1536 } : baseStatus,
    updateKnowledgeConfig: async payload => {
      savedPayloads.push(payload)
      return { ...baseStatus, status: 'unbuilt', configured: true, config: { ...baseStatus.config, model: payload.model } }
    },
    buildKnowledgeIndex: async () => {
      buildCount++
      return { ok: true, status: 'ready', chunk_count: 20, vector_count: 20, embedding_model: 'BAAI/bge-m3', embedding_dimensions: 1536, corpus_hash: 'abc123', built_at: '2026-11-26T00:00:00Z', duration_ms: 42, message: '构建成功' }
    },
  }, 'components')
  context.after(kb.dispose)

  await kb.loadStatus()
  assert.equal(kb.status.value.chunk_count, 20)
  kb.form.model = 'BAAI/bge-m3'
  await kb.buildVectors()
  assert.equal(savedPayloads.length, 1)
  assert.equal(savedPayloads[0].model, 'BAAI/bge-m3')
  assert.equal(buildCount, 1)
  assert.equal(kb.status.value.ready, true)
  assert.equal(kb.status.value.retrieval_mode, 'vector')
})

test('AssistantAvatar renders compact 30px robotic LED eyes-and-mouth expressions without head outline', () => {
  const avatarSfc = fs.readFileSync(path.join(__dirname, '../src/components/AssistantAvatar.vue'), 'utf8')
  const chatSfc = fs.readFileSync(path.join(__dirname, '../src/components/TaskChat.vue'), 'utf8')
  assert.match(avatarSfc, /size:\s*30/)
  assert.match(avatarSfc, /viewBox="0 0 36 36"/)
  assert.match(avatarSfc, /face-led-eye/)
  assert.match(avatarSfc, /face-eq-bar/)
  assert.doesNotMatch(avatarSfc, /robot-head|robot-visor|robot-ear|robot-antenna/)
  assert.match(chatSfc, /<AssistantAvatar\s+:size="30"\s+:state="avatarState"\s*\/>/)
})

test('ModelTestingView allows enqueuing additional models while probing and updates each result immediately', async context => {
  const inflight = new Map()
  const modelEntry = id => ({
    id,
    owned_by: 'acme',
    created: null,
    model_type: 'chat',
    type_source: 'inferred',
    availability: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
    tool_calling: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
    streaming: { status: 'untested', source: 'untested', latency_ms: null, ttft_ms: null, detail: null },
    structured_output: { status: 'untested', source: 'untested', latency_ms: null, detail: null },
    input_modalities: [{ modality: 'text', status: 'inferred', source: 'inferred', detail: null }],
    output_modalities: [{ modality: 'text', status: 'inferred', source: 'inferred', detail: null }],
    raw_metadata: {},
  })

  const page = load('ModelTestingView', [
    'form', 'checks', 'models', 'selectedIds', 'probing', 'probingStates',
    'probingCompleted', 'probingTotal', 'activeProbingCount', 'queuedProbingCount',
    'selectableProbeIds', 'runProbe',
  ], {
    getLLMConfig: async () => null,
    probeProviderModelsStream: async (body, onFrame) => {
      const modelId = body.models[0]
      const gate = deferred()
      inflight.set(modelId, { gate, onFrame })
      onFrame({ type: 'model_start', model_id: modelId })
      return gate.promise
    },
    useToastStore: () => ({ push: () => {} }),
  })
  context.after(page.dispose)

  const finishModel = (id, latencyMs) => {
    const entry = inflight.get(id)
    assert.ok(entry, `expected ${id} to be in-flight`)
    const profile = {
      ...modelEntry(id),
      availability: { status: 'available', source: 'probed', latency_ms: latencyMs, detail: '可用' },
      tool_calling: { status: 'supported', source: 'probed', latency_ms: latencyMs + 10, detail: '支持' },
    }
    entry.onFrame({
      type: 'model_result',
      model_id: id,
      model: profile,
      completed: 1,
      total: 1,
    })
    entry.gate.resolve({
      ok: true,
      protocol: 'openai',
      base_url: 'https://queue.example/v1',
      tested_count: 1,
      available_count: 1,
      tool_supported_count: 1,
      message: `已完成 ${id} 探测`,
      results: [profile],
    })
  }

  page.form.baseUrl = 'https://queue.example/v1'
  page.models.value = ['m1', 'm2', 'm3', 'm4', 'm5'].map(modelEntry)
  page.selectedIds.value = ['m1', 'm2']

  // 1. 先点单个模型 m1：仅 m1 处于探测中，其它模型仍可点击加入队列
  const p1 = page.runProbe(['m1'])
  await vue.nextTick()
  assert.equal(page.probing.value, true)
  assert.equal(page.probingStates.value.m1, 'probing')
  assert.equal(page.probingStates.value.m2, undefined)
  assert.deepEqual(page.selectableProbeIds.value, ['m2'])

  const firstHtml = await page.render()
  assert.match(firstHtml, /class="probe-spinner"/)
  assert.match(firstHtml, /加入探测队列（\+1）/)
  // m2 的行内按钮不应被禁用
  assert.match(firstHtml, /class="btn btn-sm row-probe-btn">(?:<!--v-if-->)?<span>探测<\/span>/)

  // 2. 在 m1 尚未返回时，继续依次点击 m2、m3、m4、m5 加入消费队列
  const p2 = page.runProbe(['m2'])
  const p3 = page.runProbe(['m3'])
  const p4 = page.runProbe(['m4'])
  const p5 = page.runProbe(['m5'])
  await vue.nextTick()

  assert.equal(page.probingTotal.value, 5)
  assert.equal(page.activeProbingCount.value, 4)
  assert.equal(page.queuedProbingCount.value, 1)
  assert.equal(page.probingStates.value.m5, 'queued')

  // 3. m2 率先完成：仅 m2 立即更新结果并释放等待态，m1 仍在探测中，且 m5 自动出队补位开始探测
  finishModel('m2', 95)
  await p2
  await vue.nextTick()

  assert.equal(page.probingCompleted.value, 1)
  assert.equal(page.probingStates.value.m2, undefined)
  assert.equal(page.probingStates.value.m1, 'probing')
  assert.equal(page.probingStates.value.m5, 'probing')
  assert.equal(page.queuedProbingCount.value, 0)
  assert.equal(page.models.value.find(m => m.id === 'm2').availability.status, 'available')
  assert.equal(page.models.value.find(m => m.id === 'm1').availability.status, 'untested')

  const partialHtml = await page.render()
  assert.match(partialHtml, /可用 · 95ms/)
  assert.match(partialHtml, /探测中/)

  // 4. 其余模型陆续完成，队列自然清空
  finishModel('m1', 510)
  finishModel('m3', 320)
  finishModel('m4', 280)
  finishModel('m5', 140)
  await Promise.all([p1, p3, p4, p5])
  await vue.nextTick()

  assert.equal(page.probing.value, false)
  assert.deepEqual(page.probingStates.value, {})
  assert.equal(page.probingCompleted.value, 5)
  assert.ok(page.models.value.every(m => m.availability.status === 'available'))

  const sfc = fs.readFileSync(path.join(__dirname, '../src/views/ModelTestingView.vue'), 'utf8')
  assert.match(sfc, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.probe-spinner[\s\S]*animation-iteration-count:\s*infinite/)
})
