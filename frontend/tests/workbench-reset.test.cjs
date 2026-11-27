const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const ts = require('typescript')

test('reset removes workbench preferences and cache but preserves model and history data', async () => {
  const source = readFileSync(join(__dirname, '../src/lib/workbenchReset.ts'), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } })
  const exports = {}
  new Function('exports', outputText)(exports)
  const retained = ['api-agent.chat-history.console', 'api-agent.chat.console.some-id',
    'api-agent.chat-execution.console.some-id', 'api-agent.llm', 'unrelated']
  const removed = ['api-agent.layout.console.chat', 'api-agent.test-settings', 'api-agent.settings.model_testing',
    'api-agent.console.settings', 'api-agent.websocket.send-interval-ms', 'api-agent.theme',
    'api-agent.sidebar-collapsed', 'api-agent.palette', 'api-agent.engine', 'api-agent.url-prefixes']
  const keys = new Set([...removed, ...retained])
  const caches = new Set(['test-cache'])
  await exports.resetWorkbench({
    get length() { return keys.size }, key: index => [...keys][index], removeItem: key => keys.delete(key),
  }, { keys: async () => [...caches], delete: async key => caches.delete(key) })
  assert.deepEqual([...keys], retained)
  assert.equal(caches.size, 0)
})
