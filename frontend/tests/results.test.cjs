const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')

const source = fs.readFileSync(path.join(__dirname, '../src/lib/results.ts'), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText
const api = {}
new Function('exports', js)(api)

test('case outcomes keep execution errors and unknowns distinct from failed assertions', () => {
  assert.deepEqual(['passed', 'failed', 'error', 'timed_out', 'skipped', 'unknown'].map(status => api.caseVerdict(status).tone),
    ['success', 'danger', 'danger', 'warning', 'warning', 'neutral'])
  assert.notEqual(api.caseVerdict('failed').label, api.caseVerdict('error').label)
  assert.deepEqual(api.countCaseOutcomes([{status: 'passed'}, {status: 'failed'}, {status: 'skipped'}, {status: 'future_status'}])
    .filter(({ count }) => count > 0).map(({ label, count }) => [label, count]),
    [['通过', 1], ['未通过', 1], ['已跳过', 1], ['未评估', 1]])
})

test('workflow completion is informational, not a passing verdict', () => {
  assert.equal(api.taskProgress('completed').tone, 'info')
  assert.equal(api.taskProgress('timed_out').tone, 'warning')
  assert.equal(api.taskProgress('future_status').tone, 'neutral')
})

test('a local target timeout has its own 504 label without inventing target HTTP', () => {
  const shown = api.presentResult({ status: 'timed_out', error_kind: 'request_timeout',
    result_code: 'REQUEST_TIMEOUT', result_status: 504 })
  assert.equal(shown.title, '请求超时')
  assert.equal(shown.resultStatus, 504)
  assert.equal(shown.tone, 'warning')
})

test('an actual HTTP 504 response remains a target response, not a missing-response timeout', () => {
  const shown = api.presentResult({ ok: true, result_code: 'HTTP_RESPONSE', result_status: 200 }, 504)
  assert.equal(shown.code, 'HTTP_RESPONSE')
  assert.equal(shown.title, '目标返回 HTTP 504')
  assert.equal(shown.tone, 'danger')
})

test('legacy timeout history and cancellation can be displayed without new fields', () => {
  assert.equal(api.presentResult({ ok: false, error_kind: 'request_timeout' }).title, '请求超时')
  const cancelled = api.presentResult({ status: 'cancelled' })
  assert.equal(cancelled.title, '已取消')
  assert.equal(cancelled.tone, 'neutral')
  assert.equal(cancelled.resultStatus, null)
})

test('an unassessed assertion is not painted as a failed assertion', () => {
  assert.deepEqual(api.assertionVerdict(null), { label: '未评估', symbol: '—', tone: 'neutral' })
  assert.equal(api.assertionVerdict(false).label, '未通过')
  assert.equal(api.assertionVerdict(true).label, '通过')
})

test('a new server result code stays visible instead of becoming success', () => {
  const shown = api.presentResult({ result_code: 'FUTURE_RESULT', result_status: null })
  assert.match(shown.title, /FUTURE_RESULT/)
  assert.notEqual(shown.tone, 'success')
})

test('management failures distinguish browser deadline from target timeout and cancellation', () => {
  assert.equal(api.presentManagementError('client_timeout', 0).code, 'BROWSER_TIMEOUT')
  assert.equal(api.presentManagementError('client_timeout', 0).resultStatus, null)
  assert.equal(api.presentManagementError('request_cancelled', 0).tone, 'neutral')
  assert.equal(api.presentManagementError('network_error', 0).code, 'MANAGEMENT_CONNECTION_FAILED')
  assert.equal(api.presentManagementError('target_not_loopback', 400).code, 'POLICY_REJECTED')
  assert.equal(api.presentResult({ error_kind: 'model_authentication_failed' }).code, 'MODEL_AUTH_FAILED')
  assert.equal(api.presentResult({ status: 'completed' }).code, 'COMPLETED')
})

test('a 3xx response is an informational target result, not a failure', () => {
  const shown = api.presentResult({ ok: true, result_code: 'HTTP_RESPONSE', result_status: 200 }, 302)
  assert.equal(shown.code, 'HTTP_RESPONSE')
  assert.equal(shown.title, '目标返回 HTTP 302')
  assert.equal(shown.tone, 'info')
})

test('1xx stays neutral so it is never mistaken for a success', () => {
  const shown = api.presentResult({ ok: true, result_code: 'HTTP_RESPONSE' }, 101)
  assert.equal(shown.tone, 'neutral')
})

test('the semantic result code is labelled so it cannot be read as a target HTTP status', () => {
  assert.equal(api.resultCodeLabel(422), '结果码 422')
  assert.equal(api.resultCodeLabel(null), null)
  assert.equal(api.resultCodeLabel(undefined), null)
})
