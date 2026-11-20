const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }

function load(file, fetch = () => { throw Error('unexpected HTTP request') }) {
  const timers = new Map(), unmount = []
  let id = 0
  const window = {
    setTimeout: fn => { timers.set(++id, fn); return id },
    clearTimeout: key => timers.delete(key),
  }
  const source = fs.readFileSync(path.join(__dirname, '../src', file), 'utf8')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const exports = {}
  new Function('require', 'exports', 'window', 'fetch', js)(
    name => { assert.equal(name, 'vue'); return { ...vue, onBeforeUnmount: fn => unmount.push(fn) } },
    exports, window, fetch,
  )
  return { ...exports, timers, unmount, tick() {
    const [key, fn] = [...timers][0] ?? []
    assert.equal(typeof fn, 'function', 'expected a scheduled callback')
    timers.delete(key)
    fn()
  } }
}

test('restarting an in-flight poll resumes the new generation without concurrent GETs', async () => {
  const first = deferred(), second = deferred(), values = []
  let calls = 0
  const module = load('composables/usePolling.ts')
  const poll = module.usePolling({
    fetch: () => ++calls === 1 ? first.promise : second.promise,
    onResult: value => values.push(value), isTerminal: () => false, intervalMs: 10,
  })
  poll.start()
  poll.start()
  assert.equal(calls, 1)
  first.resolve('stale')
  await settle()
  if (calls === 1 && module.timers.size) { module.tick(); await settle() }
  assert.equal(calls, 2, 'new generation must resume once the old request settles')
  second.resolve('current')
  await settle()
  assert.deepEqual(values, ['current'])
  poll.stop()
})

test('unmount aborts an in-flight poll and suppresses its late result', async () => {
  const pending = deferred(), values = []
  let signal
  const module = load('composables/usePolling.ts')
  module.usePolling({ fetch: current => { signal = current; return pending.promise },
    onResult: value => values.push(value), isTerminal: () => false, intervalMs: 10 }).start()
  module.unmount.forEach(fn => fn())
  pending.resolve('late')
  await settle()
  assert.equal(signal.aborted, true)
  assert.deepEqual(values, [])
  assert.equal(module.timers.size, 0)
})

test('a pre-aborted signal never starts an HTTP request', async () => {
  let calls = 0
  const module = load('api/client.ts', async () => {
    calls++
    return new Response('{}')
  })
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(module.request('/fixture', { signal: controller.signal }),
    error => error.code === 'request_cancelled')
  assert.equal(calls, 0)
})

test('HTTP timeout remains active while reading the response body', async () => {
  const body = deferred()
  const module = load('api/client.ts', async (_url, options) => {
    options.signal.addEventListener('abort', () => body.reject(Error('body aborted')))
    return { ok: true, text: () => body.promise }
  })
  const reading = module.request('/fixture')
  await settle()
  assert.equal(module.timers.size, 1, 'headers do not complete the request')
  module.tick()
  await assert.rejects(reading, error => error.code === 'client_timeout' && error.status === 0)
  assert.equal(module.timers.size, 0)
})

test('caller cancellation during body reading is distinct from the browser deadline', async () => {
  const body = deferred(), caller = new AbortController()
  const module = load('api/client.ts', async (_url, options) => {
    options.signal.addEventListener('abort', () => body.reject(Error('cancelled')))
    return { ok: true, text: () => body.promise }
  })
  const reading = module.request('/fixture', { signal: caller.signal })
  await settle()
  caller.abort()
  await assert.rejects(reading, error => error.code === 'request_cancelled')
  assert.equal(module.timers.size, 0)
})

test('console preserves a structured 504 result so it can be displayed and saved', async () => {
  const result = { ok: false, request: { method: 'GET', url: '/fixture' }, response: null,
    error_kind: 'request_timeout', result_code: 'REQUEST_TIMEOUT', result_status: 504, elapsed_ms: 50 }
  const module = load('api/client.ts', async () => new Response(JSON.stringify(result), { status: 504 }))
  assert.deepEqual(await module.request('/fixture', { acceptResultErrors: true }), result)
})

test('console opt-in does not hide a management validation or policy rejection', async () => {
  const module = load('api/client.ts', async () => new Response(JSON.stringify({
    error: { code: 'target_not_allowed', message: 'Target rejected' },
  }), { status: 400 }))
  await assert.rejects(module.request('/fixture', { acceptResultErrors: true }),
    error => error.status === 400 && error.code === 'target_not_allowed')
})

test('live console publishes split UTF-8 events before the result arrives', async () => {
  const events = [], observed = deferred()
  let stream
  const body = new ReadableStream({ start: controller => { stream = controller } })
  const module = load('api/client.ts', async () => new Response(body, {
    headers: { 'Content-Type': 'application/x-ndjson' },
  }))
  const reading = module.request('/fixture', {
    readResponse: response => module.readConsoleFrames(response, frame => {
      events.push(frame)
      if (frame.type === 'sse_event') observed.resolve()
    }),
  })
  const result = { ok: true }
  const first = { type: 'sse_event', event: { event: 'message', data: '中文' } }
  const bytes = Buffer.from(JSON.stringify(first) + '\n')
  const split = bytes.indexOf(Buffer.from('中')) + 1
  stream.enqueue(bytes.subarray(0, split))
  stream.enqueue(bytes.subarray(split))
  await observed.promise
  assert.deepEqual(events, [first])
  stream.enqueue(Buffer.from(JSON.stringify({ type: 'result', result }) + '\n'))
  assert.deepEqual(await reading, result)
  assert.equal(module.timers.size, 0)
})

test('live console reports an interrupted stream when no final frame arrives', async () => {
  const module = load('api/client.ts', async () => new Response('{"type":"sse_start"}\n', {
    headers: { 'Content-Type': 'application/x-ndjson' },
  }))
  await assert.rejects(module.request('/fixture', {
    readResponse: response => module.readConsoleFrames(response, () => {}),
  }), error => error.code === 'incomplete_stream')
})
