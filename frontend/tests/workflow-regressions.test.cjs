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
const chatHistoryExports = {}
new Function('exports', ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/lib/chatHistory.ts'), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(chatHistoryExports)
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
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('execution promotion moves the assistant transcript and removes the AI-only label', () => {
  const storage = memoryStorage({
    'api-agent.chat.console.chat-one': '[{"role":"user","content":"你好"}]',
    'api-agent.chat-history.console': JSON.stringify([{ id: 'chat-one', title: '问候', created_at: '2026-11-20T00:00:00Z' }]),
  })
  const previousWindow = globalThis.window
  globalThis.window = { localStorage: storage }
  try {
    chatHistoryExports.attachChatToExecution('console', 'chat-one', 'request-one')
    assert.equal(storage.getItem('api-agent.chat-execution.console.request-one'), 'chat-one')
    assert.equal(chatHistoryExports.chatTranscriptKey('console', 'request-one'), 'api-agent.chat.console.chat-one')
    assert.equal(storage.getItem('api-agent.chat.console.chat-one'), '[{"role":"user","content":"你好"}]')
    assert.deepEqual(chatHistoryExports.listChatHistories('console'), [])
  } finally {
    globalThis.window = previousWindow
  }
})

function load(name, names, mocks = {}, directory = 'views') {
  const sfc = fs.readFileSync(path.join(__dirname, '../src', directory, `${name}.vue`), 'utf8')
  const source = sfc.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/g, '')
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
    ...chatHistoryExports,
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
    [1200, 200, 10000, 60000, 20000],
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
    for (const [raw, expected] of [[max + 1, max], [min - 1, min]]) {
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
    defineProps: () => ({ scope: 'tasks', conversationKey: 'history-one' }),
    window: { localStorage: storage },
    useId: () => 'assistant-model-menu',
    getLLMConfig: async () => ({ model: savedModel }),
    selectLLMModel: async model => ({ model: (savedModel = model) }),
    generateChatTitle: async () => ({ title: 'AI 对话测试' }),
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

test('the first assistant message creates one history and later turns keep its id', async context => {
  const storage = memoryStorage()
  const created = []
  const titled = []
  const previousWindow = globalThis.window
  globalThis.window = { localStorage: storage }
  context.after(() => { globalThis.window = previousWindow })
  const assistant = load('TaskChat', ['send', 'input', 'activeConversationKey'], {
    defineProps: () => ({ scope: 'console', conversationKey: 'draft-new' }),
    defineEmits: () => (...args) => {
      if (args[0] === 'history-created') created.push(args[1])
      if (args[0] === 'history-titled') titled.push(args[1])
    },
    window: { localStorage: storage },
    useId: () => 'chat-test',
    getLLMConfig: async () => ({ model: 'fixture' }),
    generateChatTitle: async () => ({ title: 'SSE 接口测试问题' }),
    chatWithAssistant: async (_taskId, messages) => `答复：${messages.at(-1).content}`,
  }, 'components')
  context.after(assistant.dispose)
  assistant.input.value = '请解释 SSE 测试结果'
  await assistant.send()
  assert.equal(created.length, 1)
  assert.equal(created[0].title, 'AI 对话')
  const firstId = created[0].id
  assert.equal(assistant.activeConversationKey.value, firstId)
  assert.equal(JSON.parse(storage.getItem('api-agent.chat-history.console'))[0].title, 'SSE 接口测试问题')
  assert.equal(titled[0].title, 'SSE 接口测试问题')
  assistant.input.value = '再补充一下'
  await assistant.send()
  assert.equal(created.length, 1)
  assert.equal(titled.length, 1)
  assert.equal(assistant.activeConversationKey.value, firstId)
  assert.equal(JSON.parse(storage.getItem('api-agent.chat-history.console'))[0].title, 'SSE 接口测试问题')
  assert.equal(JSON.parse(storage.getItem('api-agent.chat-history.console')).length, 1)
  assert.equal(JSON.parse(storage.getItem(`api-agent.chat.console.${firstId}`)).length, 4)
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
  assert.equal(first.taskSettings.allowWrites, false)
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
