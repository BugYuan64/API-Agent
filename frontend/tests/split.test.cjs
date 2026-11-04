/**
 * 分栏阈值与持久化的行为回归。
 *
 * 覆盖用户明确要求的语义：拖到阈值后不再变化、另一侧不会被挤没、
 * 刷新后尺寸能恢复、存储不可用时仍可拖动。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')

function loadSplit(storage) {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/split.ts'), 'utf8')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const exports = {}
  new Function('exports', 'window', js)(exports, { localStorage: storage })
  return exports
}

const memoryStorage = () => {
  const map = new Map()
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
  }
}

const throwingStorage = () => ({
  getItem: () => { throw new Error('denied') },
  setItem: () => { throw new Error('denied') },
})

test('a drag past the lower threshold stops at the minimum', () => {
  const { clampSize } = loadSplit(memoryStorage())
  assert.equal(clampSize({ size: 100, min: 220, available: 1200 }), 220)
  assert.equal(clampSize({ size: -400, min: 220, available: 1200 }), 220)
})

test('a drag past the upper threshold stops before squeezing the other pane', () => {
  const { clampSize } = loadSplit(memoryStorage())
  // 容器 1200，另一侧最小 360 → 本侧最多 840
  assert.equal(clampSize({ size: 1100, min: 220, minOther: 360, available: 1200 }), 840)
  assert.equal(clampSize({ size: 2000, min: 220, minOther: 360, available: 1200 }), 840)
})

test('an explicit max caps the size even when the container is wide', () => {
  const { clampSize } = loadSplit(memoryStorage())
  assert.equal(clampSize({ size: 900, min: 220, max: 600, minOther: 100, available: 2000 }), 600)
})

test('a container too small for both minimums still respects the primary minimum', () => {
  const { clampSize } = loadSplit(memoryStorage())
  // 总宽 500，两侧最小 220+360 已放不下：仍返回本侧最小值而不是更小
  assert.equal(clampSize({ size: 300, min: 220, minOther: 360, available: 500 }), 220)
})

test('unmeasured container applies only min/max, not the other-pane limit', () => {
  const { clampSize } = loadSplit(memoryStorage())
  assert.equal(clampSize({ size: 500, min: 220, minOther: 360, available: 0 }), 500)
})

test('saved size is restored, and missing or invalid values fall back', () => {
  const storage = memoryStorage()
  const { readStoredSize, writeStoredSize } = loadSplit(storage)
  assert.equal(readStoredSize('k', 300), 300)
  writeStoredSize('k', 417)
  assert.equal(readStoredSize('k', 300), 417)
  storage.setItem('k', 'not-a-number')
  assert.equal(readStoredSize('k', 300), 300)
  storage.setItem('k', '-5')
  assert.equal(readStoredSize('k', 300), 300)
  assert.equal(readStoredSize(undefined, 300), 300)
})

test('storage being unavailable never blocks dragging', () => {
  const { readStoredSize, writeStoredSize } = loadSplit(throwingStorage())
  assert.equal(readStoredSize('k', 300), 300)
  assert.doesNotThrow(() => writeStoredSize('k', 400))
})

test('each workspace key keeps its own size', () => {
  const storage = memoryStorage()
  const { readStoredSize, writeStoredSize } = loadSplit(storage)
  writeStoredSize('api-agent.layout.console.history', 240)
  writeStoredSize('api-agent.layout.long-connection.history', 300)
  assert.equal(readStoredSize('api-agent.layout.console.history', 268), 240)
  assert.equal(readStoredSize('api-agent.layout.long-connection.history', 268), 300)
})

test('the hit band is wide enough to grab without pixel-perfect aiming', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/components/ResizableSplit.vue'), 'utf8')
  // 命中带必须覆盖整条边界：水平分栏占满高度、垂直分栏占满宽度。
  assert.match(source, /\.split-horizontal > \.split-handle \{[\s\S]*?top: 0;[\s\S]*?bottom: 0;/)
  assert.match(source, /\.split-vertical > \.split-handle \{[\s\S]*?left: 0;[\s\S]*?right: 0;/)
  // 且不能窄到需要精确命中（>= 8px）。
  const width = source.match(/\.split-horizontal > \.split-handle \{[\s\S]*?width: (\d+)px;/)
  assert.ok(width && Number(width[1]) >= 8, 'horizontal hit band must be at least 8px wide')
})

test('dragging requires a hold, and drift during the hold cancels it', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/components/ResizableSplit.vue'), 'utf8')
  const start = source.slice(source.indexOf('function start('), source.indexOf('function keydown('))
  // 长按门槛存在：按下先进入 holding，超时才 dragging。
  assert.match(start, /holding\.value = true/)
  assert.match(start, /setTimeout/)
  assert.ok(/HOLD_MS/.test(source), 'hold threshold must be a named constant')

  // 待命期明显移动（> 6px）应取消长按，避免把滑选文本当成拖动。
  const bind = source.slice(source.indexOf('function bindSession('), source.indexOf('function endSession('))
  assert.match(bind, /if \(holding\.value\)/)
  assert.match(bind, /drift > 6/)
  assert.match(bind, /endSession\(id\)/)
})

test('a drag session always ends even if the pointer leaves the handle', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/components/ResizableSplit.vue'), 'utf8')
  // 回归来源：pointer capture 后指针移出 handle，浏览器不再向 handle 派发
  // pointerup，dragging 卡死、body 上的 split-dragging 类永不移除。
  // 修复约定：会话监听必须挂 window，且 endSession 无条件清理状态。
  assert.match(source, /window\.addEventListener\('pointerup'/)
  assert.match(source, /window\.addEventListener\('pointercancel'/)
  const end = source.slice(source.indexOf('function endSession('), source.indexOf('function start('))
  assert.match(end, /dragging\.value = false/)
  assert.match(end, /document\.body\.classList\.remove\('split-dragging'\)/)
  assert.match(end, /sessionCleanup\?\.\(\)/)
  // 模板上不能再挂 move/up（会被 capture 重定向吞掉）。
  const template = source.slice(source.indexOf('<template>'))
  assert.ok(!/@pointermove|@pointerup/.test(template), 'session events must live on window, not the handle')
})

test('the separator uses resize cursors and a visible arrow affordance', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/components/ResizableSplit.vue'), 'utf8')

  // 光标：自定义 SVG 双向箭头（部分宿主不渲染系统 resize 光标），保留系统值作回退。
  assert.match(source, /cursor: url\("data:image\/svg\+xml,/)
  assert.match(source, /col-resize/)
  assert.match(source, /row-resize/)

  // 同时保留边界中央的可见箭头：即使宿主不绘制自定义光标，用户也能看到。
  assert.match(source, /class="grip-arrow"/)
  // 箭头常驻可见（淡显），悬浮/待命/拖动时变实。
  assert.match(source, /opacity: 0\.45/)
  assert.match(source, /\.split-handle:hover \.grip-arrow/)

  // 边界是一条 1px 细线，始终可见，用来分隔两个模块。
  assert.match(source, /\.split-horizontal \.split-grip \{[\s\S]*?width: 1px;/)

  // 不用抓手光标。
  assert.ok(!/cursor: (grab|grabbing)/.test(source), 'must not use grab cursors')
})

test('manual pane has an independent editor|result split', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/views/ManualPane.vue'), 'utf8')
  assert.match(source, /persist-key="api-agent\.layout\.manual\.editor"/)
  assert.match(source, /:min="320"/)
  assert.match(source, /:min-other="300"/)
})

test('long connection has an independent config|stage split', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/views/LongConnection.vue'), 'utf8')
  assert.match(source, /persist-key="api-agent\.layout\.long-connection\.config"/)
  assert.match(source, /:min="280"/)
  assert.match(source, /:min-other="360"/)
})

test('the case-test page owns a single shared history column for both modes', () => {
  const read = (name) => fs.readFileSync(path.join(__dirname, '../src/views', name), 'utf8')
  const shell = read('TestCasesView.vue')
  const manual = read('ManualPane.vue')
  const auto = read('AutoPane.vue')
  const shared = fs.readFileSync(
    path.join(__dirname, '../src/components/HistoryList.vue'), 'utf8')

  // 外壳持有唯一的工作台与历史列：切换模式只替换右侧内容。
  assert.match(shell, /<TestWorkspace[^>]*persist-key="api-agent\.layout\.cases\.history"/)
  assert.match(shell, /<template #history>/)
  assert.equal((shell.match(/<HistoryList/g) ?? []).length, 1,
    'history column must be rendered exactly once')

  // 两个面板不再自己带工作台或历史列，只上报条目。
  for (const [name, src] of [['manual', manual], ['auto', auto]]) {
    assert.ok(!/<TestWorkspace/.test(src), `${name} must not nest its own workspace`)
    assert.ok(!/<HistoryList/.test(src), `${name} must not render its own history`)
    assert.match(src, /defineEmits/, `${name} must emit history entries`)
    assert.match(src, /defineExpose\(\{ openHistory \}\)/, `${name} must expose openHistory`)
  }

  // 排版与尺寸只有一份定义。
  assert.match(shared, /\.history-column \{[\s\S]*?border-right: 1px solid/)
  assert.match(shared, /\.history-title \{[\s\S]*?font-size: 10\.5px;/)
})

test('the history width is shared across modes, not per-mode state', () => {
  const shell = fs.readFileSync(
    path.join(__dirname, '../src/views/TestCasesView.vue'), 'utf8')
  // 只有外壳持有布局键；面板不得再声明自己的 history 键（否则拖动不共享）。
  assert.match(shell, /api-agent\.layout\.cases\.history/)
  for (const name of ['ManualPane.vue', 'AutoPane.vue']) {
    const src = fs.readFileSync(path.join(__dirname, '../src/views', name), 'utf8')
    assert.ok(!/layout\.manual\.history|layout\.auto\.history/.test(src),
      `${name} must not keep a per-mode history layout key`)
  }
})


test('case-test history is one merged list, not per-mode state', () => {
  const shell = fs.readFileSync(
    path.join(__dirname, '../src/views/TestCasesView.vue'), 'utf8')

  // 标题与空状态必须是固定文案，不能按模式分支——
  // 否则同一个历史列在两种模式下会显示不同文字，违背“一份历史”。
  assert.ok(!/historyTitle/.test(shell), 'title must not be mode-dependent')
  assert.ok(!/historyEmptyText/.test(shell), 'empty text must not be mode-dependent')
  assert.match(shell, /title="测试历史"/)

  // 两个来源合并成一个按时间倒序的列表。
  assert.match(shell, /\[\.\.\.manualEntries\.value, \.\.\.autoEntries\.value\]/)
  assert.match(shell, /\.sort\(/)

  // 点击按 source 分派回对应面板。
  assert.match(shell, /entry\.source === 'auto'/)
})

test('each pane tags its entries with a source discriminator', () => {
  const manual = fs.readFileSync(path.join(__dirname, '../src/views/ManualPane.vue'), 'utf8')
  const auto = fs.readFileSync(path.join(__dirname, '../src/views/AutoPane.vue'), 'utf8')
  assert.match(manual, /source: 'manual'/)
  assert.match(auto, /source: 'auto'/)
})

test('the settings page is a scrollable long form', () => {
  const src = fs.readFileSync(
    path.join(__dirname, '../src/views/SettingsView.vue'), 'utf8')
  const block = src.slice(src.indexOf('.settings-page {'), src.indexOf('.settings-container {'))
  // 回归来源：根容器曾是 display:flex + height:100%，内容溢出但不改变 scrollHeight，
  // 导致 API Key、获取模型列表、退出按钮全部落在视口外且不可点。
  assert.ok(!/display:\s*flex/.test(block), 'settings root must not be a flex container')
  // 用行首锚定，避免把 `min-height: 100%` 误判为固定高度。
  assert.ok(!/^\s*height:\s*100%/m.test(block), 'settings root must not lock height to 100%')
  assert.match(block, /min-height:\s*100%/, 'settings root must grow with its content')
})

test('model field shows exactly one control', () => {
  const src = fs.readFileSync(path.join(__dirname, '../src/views/SettingsView.vue'), 'utf8')
  const row = src.slice(src.indexOf('<div class="model-input-row">'), src.indexOf('<span class="field-hint">', src.indexOf('<div class="model-input-row">')))
  // 回归来源：拉取模型列表后文本框与下拉框同时出现，等于两个输入点。
  // 约定：二者互斥——有列表时只给下拉框，无列表时只给文本框。
  // select 与 input 互为 v-if / v-else：同一时刻只渲染一个控件。
  assert.match(row, /<select\s+\s*v-if="availableModels\.length > 0"/)
  assert.match(row, /<input\s+\s*v-else/)
})

test('every engine card exposes a config entry that opens a modal', () => {
  const src = fs.readFileSync(path.join(__dirname, '../src/views/SettingsView.vue'), 'utf8')
  // 每个引擎卡片右上角都有配置按钮（不是只有 Postman）。
  const cardStart = src.indexOf('v-for="item in engines"')
  const cardEnd = src.indexOf('导入引擎（界面占位')
  const card = src.slice(cardStart, cardEnd)
  assert.match(card, /class="engine-config-btn"/)
  assert.match(card, /@click\.stop="openEngineConfig\(item\)"/)

  // 详情走弹窗：页面里不再常驻服务详情块。
  const modal = src.slice(src.indexOf('<AppModal'), src.indexOf('</AppModal>'))
  assert.match(modal, /@confirm="saveEngineConfig"/)
  assert.match(modal, /@cancel="closeEngineConfig"/)

  // 弹窗内容必须按引擎区分：不能把 postmanmcp 的详情套给所有引擎。
  assert.match(modal, /v-if="configEngineId === 'postman'"/)
  assert.match(modal, /v-else/)
  assert.match(modal, /本地原生引擎详情/)
  // 标题里引擎名与「配置」之间不加空格。
  assert.match(modal, /:title="`\$\{configEngineName\}配置`"/)
})

test('the modal component locks scroll and supports Esc', () => {
  const src = fs.readFileSync(path.join(__dirname, '../src/components/AppModal.vue'), 'utf8')
  assert.match(src, /document\.body\.style\.overflow/)
  assert.match(src, /event\.key === 'Escape'/)
  assert.match(src, /role="dialog"/)
})
