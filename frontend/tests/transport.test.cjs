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
    error => error.code === 'network_aborted')
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
  await assert.rejects(reading, error => error.code === 'network_aborted')
  assert.equal(module.timers.size, 0)
})
