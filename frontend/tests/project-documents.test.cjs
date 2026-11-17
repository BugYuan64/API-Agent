/** Execute the production document-workspace setup with Vue reactivity and controlled requests. */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const vue = require('vue')

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function workspace(overrides = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../src/components/ProjectDocuments.vue'), 'utf8')
    .split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[\s\S]*?from\s+['"][^'"]+['"]\r?\n/g, '')
  const script = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const unmounts = []
  const timers = new Map()
  let timerId = 0
  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status }
  }
  const env = {
    ...vue,
    ApiError,
    defineProps: () => ({ projectId: 'project-one' }),
    onMounted: () => {},
    onBeforeUnmount: callback => unmounts.push(callback),
    describeError: error => error.message,
    useToastStore: () => ({ push: () => {} }),
    window: {
      setTimeout: callback => { timers.set(++timerId, callback); return timerId },
      clearTimeout: id => timers.delete(id),
    },
    readProjectSource: async () => { throw new ApiError(404, 'Source not found') },
    listProjectDocuments: async () => ({ items: [] }),
    readProjectDocument: async () => { throw new Error('Unexpected detail request') },
    ...overrides,
  }
  const pollingSource = fs.readFileSync(path.join(__dirname, '../src/composables/usePolling.ts'), 'utf8')
  const pollingScript = ts.transpileModule(pollingSource, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const pollingExports = {}
  new Function('require', 'exports', 'window', pollingScript)(
    () => ({ ...vue, onBeforeUnmount: callback => unmounts.push(callback) }), pollingExports, env.window,
  )
  env.usePolling = pollingExports.usePolling
  const scope = vue.effectScope()
  const names = ['source', 'documents', 'current', 'error', 'showImport', 'canGenerate',
    'loading', 'selectionError', 'load', 'openDocument', 'generate', 'cancel', 'polling', 'download', 'exporting']
  const api = scope.run(() => new Function(...Object.keys(env),
    `${script}; return { ${names.join(',')} }`)(...Object.values(env)))
  return { ...api, dispose: () => { unmounts.forEach(callback => callback()); scope.stop() } }
}

function record(id, status = 'completed') {
  return { id, project_id: 'project-one', source_id: 'source-one', status,
    created_at: '2026-11-17T00:00:00Z', updated_at: '2026-11-17T00:00:00Z',
    processed_files: 2, total_files: 2, processed_batches: 1, total_batches: 1,
    endpoints: [], warnings: [], error: null, markdown: '# API' }
}

test('a project without imported source opens the import form without an error', async () => {
  const page = workspace()
  await page.load()
  assert.equal(page.source.value, null)
  assert.equal(page.error.value, '')
  assert.equal(page.showImport.value, true)
  assert.equal(page.canGenerate.value, false)
  page.dispose()
})

test('switching saved documents discards a late response for the previous selection', async () => {
  const late = deferred()
  const page = workspace({
    readProjectDocument: async (_projectId, id) => id === 'old' ? late.promise : record('new'),
  })
  page.documents.value = [record('old'), record('new')]
  const pending = page.openDocument('old')
  await page.openDocument('new')
  late.resolve(record('old', 'running'))
  await pending
  assert.equal(page.current.value.id, 'new')
  assert.equal(page.polling.active.value, false)
  page.dispose()
})

test('refresh selects and resumes polling the active saved generation', async () => {
  const active = record('active', 'running')
  const page = workspace({
    listProjectDocuments: async () => ({ items: [record('old'), active] }),
    readProjectDocument: async (_projectId, id) => record(id, 'running'),
  })
  await page.load()
  assert.equal(page.current.value.id, 'active')
  assert.equal(page.polling.active.value, true)
  page.dispose()
  assert.equal(page.polling.active.value, false)
})

test('empty scans block generation and a cancelled record retains its exportable body', async () => {
  let starts = 0
  const page = workspace({
    startProjectDocument: async () => { starts++; return record('started') },
    cancelProjectDocument: async (_projectId, id) => record(id, 'cancelled'),
  })
  page.loading.value = false
  page.source.value = { id: 'source-one', complete: false, scanned_files: 0 }
  await page.generate()
  assert.equal(starts, 0)
  page.current.value = record('active', 'running')
  page.documents.value = [page.current.value]
  await page.cancel()
  assert.equal(page.current.value.status, 'cancelled')
  assert.equal(page.current.value.markdown, '# API')
  assert.equal(page.polling.active.value, false)
  page.dispose()
})

test('an incomplete scan can generate an explicitly partial document from its imported files', async () => {
  const calls = []
  const page = workspace({
    startProjectDocument: async (projectId, sourceId) => {
      calls.push({ projectId, sourceId })
      return record('partial', 'partial')
    },
  })
  page.loading.value = false
  page.source.value = { id: 'source-partial', complete: false, scanned_files: 10 }
  await page.generate()
  assert.deepEqual(calls, [{ projectId: 'project-one', sourceId: 'source-partial' }])
  assert.equal(page.current.value.status, 'partial')
  page.dispose()
})

test('an export error shows feedback and never downloads the error response', async () => {
  const notices = []
  let createdLinks = 0
  const page = workspace({
    exportProjectDocument: async () => { throw new Error('文档暂时无法读取') },
    useToastStore: () => ({ push: (...args) => notices.push(args) }),
    document: { createElement: () => { createdLinks++; return {} } },
  })
  page.current.value = record('export')
  await page.download('openapi')
  assert.equal(createdLinks, 0)
  assert.equal(page.exporting.value, false)
  assert.deepEqual(notices, [['danger', '文档导出失败', '文档暂时无法读取']])
  assert.equal(page.current.value.id, 'export')
  page.dispose()
})
