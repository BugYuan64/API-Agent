const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')

const load = (filename = 'taskResults.ts') => {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib', filename), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const api = {}
  new Function('exports', 'require', outputText)(api, specifier => {
    assert.equal(specifier, './results')
    return load('results.ts')
  })
  return api
}

const requestCase = (path, method = 'GET', protocol = 'http') => ({
  name: '重复名称', protocol, method, path, source: 'document', kind: 'document', assertions: [],
})

test('saved operations retain their template, variant cases and per-index verdicts during polling', () => {
  const { buildTaskResultTree } = load()
  const cases = [
    requestCase('/users/7?mode=short'),
    requestCase('/users/0'),
    requestCase('/users/7?mode=full'),
    requestCase('/users/me'),
  ]
  const operations = [
    { path: '/users/{id}', example_index: 2, case_indices: [1, 2] },
    { path: '/users/me', example_index: 3, case_indices: [3] },
  ]
  const failed = { index: 1, status: 'failed' }
  const passed = { index: 0, status: 'passed' }
  const groups = buildTaskResultTree({ cases, operations }, [failed, passed])
  assert.deepEqual(groups.map(group => group.path), ['/users/{id}', '/users/me'])
  assert.equal(groups[0].example, cases[2])
  assert.deepEqual(groups[0].cases.map(item => [item.index, item.result?.status]),
    [[0, 'passed'], [1, 'failed'], [2, undefined]])
  assert.equal(groups[1].cases[0].result, undefined)
})

test('each interface counts only its recorded outcomes, including legacy results and newly polled cases', () => {
  const { buildTaskResultTree } = load()
  const snapshot = { cases: [
    requestCase('/items?q=one'),
    requestCase('/items?q=two'),
    requestCase('/items?q=three'),
    requestCase('/items', 'POST'),
    requestCase('/items?archive=true', 'POST'),
    requestCase('/profiles'),
  ] }
  const results = [
    { index: 0, status: 'passed' },
    { index: 1, status: 'passed' },
    { index: 3, status: 'failed' },
    { index: 4, status: 'unrecognized_verdict' },
  ]
  const counts = records => buildTaskResultTree(snapshot, records)
    .map(group => group.outcomes.map(({ status, count }) => [status, count]))
  assert.deepEqual(counts(results), [[['passed', 2]], [['failed', 1], ['unknown', 1]], []])
  assert.deepEqual(counts([...results, { index: 2, status: 'skipped' }]),
    [[['passed', 2], ['skipped', 1]], [['failed', 1], ['unknown', 1]], []])
})

test('older history groups by protocol, method and path without treating query variants as new APIs', () => {
  const { buildTaskResultTree } = load()
  const cases = [
    requestCase('/items?q=one'),
    requestCase('/items?q=two'),
    requestCase('/items', 'POST'),
    requestCase('/items', 'GET', 'sse'),
  ]
  const groups = buildTaskResultTree({ cases }, [])
  assert.deepEqual(groups.map(group => [group.protocol, group.method, group.path, group.cases.map(item => item.index)]),
    [['http', 'GET', '/items', [0, 1]], ['http', 'POST', '/items', [2]], ['sse', 'GET', '/items', [3]]])
  assert.equal(groups[0].example, undefined)
  assert.equal(groups[0].cases[1].input.path, '/items?q=two')
})

test('stream waits track the active unfinished case and clear at completion or cancellation', () => {
  const { activeStreamWait } = load()
  const task = {
    status: 'running', stage: 'executing', current_index: 0, results: [],
    snapshot: { cases: [requestCase('/chat'), requestCase('/ws', 'GET', 'websocket')] },
  }
  assert.equal(activeStreamWait(task), null)
  const waiting = { ...task, active_stream: 'sse' }
  assert.deepEqual(activeStreamWait(waiting), { index: 0, label: '等待 SSE 流结束' })
  assert.equal(activeStreamWait({ ...waiting, results: [{ index: 0, status: 'passed' }] }), null)
  assert.equal(activeStreamWait({ ...waiting, status: 'cancelled' }), null)
  assert.equal(activeStreamWait({ ...waiting, stage: 'summarizing' }), null)
  assert.deepEqual(activeStreamWait({ ...task, current_index: 1 }),
    { index: 1, label: '等待 WebSocket 观察结束' })
})

test('contract boundary cases do not inflate the per-operation test count', () => {
  const { buildTaskDraftGroups } = load()
  const catalog = { cases: [], endpoints: [{
    ...requestCase('/items/1', 'DELETE'), path_template: '/items/{id}',
    generated_cases: [
      { ...requestCase('/items/2', 'DELETE'), source: 'generated', kind: 'contract' },
      { ...requestCase('/items/3', 'DELETE'), source: 'generated', kind: 'test' },
    ],
  }] }
  const [group] = buildTaskDraftGroups(catalog)
  assert.equal(group.testCount, 1)
  assert.deepEqual(group.cases.map(item => item.label), ['契约示例', '契约示例', 'test'])
})
