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
const vue = require('vue')

function loadSplit(storage) {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/split.ts'), 'utf8')
  const js = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const exports = {}
  new Function('exports', 'window', js)(exports, { localStorage: storage })
  return exports
}

function loadWorkspaceLayout() {
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/workspaceLayout.ts'), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const exports = {}
  new Function('exports', outputText)(exports)
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

/** 使用真实Vue计算/观察器，只替换浏览器测量和生命周期，直接执行生产SFC。 */
function mountSplit(input, storage = memoryStorage(), connection = {}) {
  const file = fs.readFileSync(path.join(__dirname, '../src/components/ResizableSplit.vue'), 'utf8')
  const script = file.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[^\n]+from[^\n]+\r?\n/g, '')
  const js = ts.transpileModule(script, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const props = vue.reactive({ direction: 'horizontal', min: 220, minOther: 360, ...input })
  const mounts = [], cleanup = []
  const scope = vue.effectScope()
  let width = 1200, height = 800
  const env = {
    ...vue, ...loadSplit(storage),
    defineProps: () => props, withDefaults: p => p,
    defineEmits: () => (event, value) => {
      if (event === 'update:size') props.size = value
      if (event === 'layout') connection.onLayout?.(value)
      if (event === 'resize') connection.onResize?.(value)
    },
    onMounted: fn => mounts.push(fn), onBeforeUnmount: fn => cleanup.push(fn),
    ResizeObserver: class { observe() {} disconnect() {} },
    window: { localStorage: storage },
    document: { body: { classList: { remove() {} } } },
  }
  const api = scope.run(() => new Function(...Object.keys(env), `${js};return {root,currentSize,readAvailable,emitSize,keydown}`)(...Object.values(env)))
  api.root.value = { getBoundingClientRect: () => ({ width, height }) }
  mounts.forEach(fn => fn())
  return {
    ...api, props, storage,
    resize: async (value, nextHeight = height) => { width = value; height = nextHeight; api.readAvailable(); await vue.nextTick() },
    dispose: () => { cleanup.forEach(fn => fn()); scope.stop() },
  }
}

test('temporary viewport compression preserves saved widths and restores the history preference', async () => {
  const pane = mountSplit({ size: 278, min: 200, minOther: 910, fitOther: 620, persistKey: 'history' })
  pane.storage.setItem('history', '278')
  await pane.resize(418)
  assert.equal(pane.currentSize.value, 102)
  assert.equal(pane.props.size, 278)
  assert.equal(pane.storage.getItem('history'), '278')
  await pane.resize(1368)
  assert.equal(pane.currentSize.value, 278)
  pane.dispose()
})

function mountWorkspace() {
  const file = fs.readFileSync(path.join(__dirname, '../src/components/TestWorkspace.vue'), 'utf8')
  const script = file.split('<script setup lang="ts">')[1].split('</script>')[0]
    .replace(/import[^\n]+from[^\n]+\r?\n/g, '')
  const { outputText } = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2022 } })
  const storage = memoryStorage()
  const layout = loadWorkspaceLayout()
  const mounts = [], cleanup = []
  const scope = vue.effectScope()
  let width = 850
  let notifyResize
  const env = {
    ...vue, ...loadSplit(storage), ...layout, bounds: layout.WORKSPACE_LAYOUT_BOUNDS,
    defineProps: () => ({ historyKey: 'history', editorKey: 'editor', editorWidth: 335 }),
    withDefaults: (props, defaults) => ({ ...defaults, ...props }),
    onMounted: fn => mounts.push(fn), onBeforeUnmount: fn => cleanup.push(fn),
    ResizeObserver: class { constructor(callback) { notifyResize = callback } observe() {} disconnect() {} },
  }
  storage.setItem('history', '205')
  storage.setItem('editor', '325')
  storage.setItem('editor.second', '320')
  const api = scope.run(() => new Function(...Object.keys(env), `${outputText};return {root,widths,historyRange,editorRange,resizeNeighbor}`)(...Object.values(env)))
  api.root.value = { getBoundingClientRect: () => ({ width }) }
  mounts.forEach(fn => fn())
  return {
    ...api, storage,
    resize: value => { width = value; notifyResize() },
    dispose: () => { cleanup.forEach(fn => fn()); scope.stop() },
  }
}

test('the shared workspace redistributes width without persisting temporary sizes and adjusts adjacent panes on drag', () => {
  const workspace = mountWorkspace()
  try {
    assert.deepEqual(workspace.widths.value, { history: 205, editor: 325, results: 320 })
    workspace.resize(840)
    assert.deepEqual(workspace.widths.value, { history: 200, editor: 320, results: 320 })
    workspace.resize(420)
    assert.deepEqual(workspace.widths.value, { history: 100, editor: 160, results: 160 })
    workspace.resize(0)
    workspace.resize(1300)
    assert.deepEqual(workspace.widths.value, { history: 200, editor: 720, results: 380 })
    assert.equal(workspace.storage.getItem('history'), '205')
    assert.equal(workspace.storage.getItem('editor'), '325')
    assert.equal(workspace.storage.getItem('editor.second'), '320')

    workspace.resizeNeighbor('editor', 660)
    assert.deepEqual(workspace.widths.value, { history: 200, editor: 660, results: 440 })
    workspace.resizeNeighbor('history', 240)
    assert.deepEqual(workspace.widths.value, { history: 240, editor: 620, results: 440 })
    assert.equal(workspace.storage.getItem('history'), '240')
    assert.equal(workspace.storage.getItem('editor'), '620')
    assert.equal(workspace.storage.getItem('editor.second'), '440')
  } finally {
    workspace.dispose()
  }
})

test('a drag-only reservation limits the outer divider without resizing it as the inner divider moves', async () => {
  const changes = []
  const pane = mountSplit({ size: 236, min: 175, minOther: 510, fitOther: 510, resizeMinOther: 920 },
    undefined, { onResize: change => changes.push(change) })
  try {
    pane.emitSize(400)
    assert.equal(pane.currentSize.value, 280)
    assert.deepEqual(changes, [{ delta: 44, remaining: 920 }])

    pane.props.resizeMinOther = 970
    await vue.nextTick()
    assert.equal(pane.currentSize.value, 280)
    assert.equal(changes.length, 1)
  } finally {
    pane.dispose()
  }
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

test('changing workspace width transfers space to editor, results, then history in either direction', () => {
  const { allocateWorkspaceWidth } = loadWorkspaceLayout()
  const initial = { history: 260, editor: 600, results: 640 }
  assert.deepEqual(allocateWorkspaceWidth(1600, initial), { history: 260, editor: 700, results: 640 })
  assert.deepEqual(allocateWorkspaceWidth(1660, initial), { history: 260, editor: 720, results: 680 })
  assert.deepEqual(allocateWorkspaceWidth(1720, initial), { history: 280, editor: 720, results: 720 })
  assert.deepEqual(allocateWorkspaceWidth(1400, initial), { history: 260, editor: 500, results: 640 })
  assert.deepEqual(allocateWorkspaceWidth(1200, initial), { history: 260, editor: 320, results: 620 })
  assert.deepEqual(allocateWorkspaceWidth(870, initial), { history: 230, editor: 320, results: 320 })
  const expanded = allocateWorkspaceWidth(1660, initial)
  assert.deepEqual(allocateWorkspaceWidth(1500, expanded), { history: 260, editor: 560, results: 680 })
  assert.deepEqual(allocateWorkspaceWidth(3000, initial), { history: 360, editor: 720, results: 720 })
  assert.deepEqual(allocateWorkspaceWidth(420, initial), { history: 100, editor: 160, results: 160 })
  assert.deepEqual(allocateWorkspaceWidth(400, initial, true), { history: 0, editor: 200, results: 200 })
})

test('workspace drag limits reserve both adjacent panes without touching the third', () => {
  const { adjacentWidthRange } = loadWorkspaceLayout()
  const widths = { history: 260, editor: 700, results: 400 }
  assert.deepEqual(adjacentWidthRange(widths, 'history'), { min: 240, max: 360 })
  assert.deepEqual(adjacentWidthRange(widths, 'editor'), { min: 380, max: 720 })
  assert.deepEqual(adjacentWidthRange({ history: 100, editor: 160, results: 160 }, 'editor'), { min: 160, max: 160 })
})

test('assistant height obeys both shared bounds including keyboard and restored sizes', async () => {
  const { WORKSPACE_LAYOUT_BOUNDS: bounds } = loadWorkspaceLayout()
  const storage = memoryStorage()
  storage.setItem('upper', '240')
  const pane = mountSplit({ direction: 'vertical', size: 260, min: bounds.upperMinHeight,
    minOther: bounds.assistantHeight.min, maxOther: bounds.assistantHeight.max, fitOther: bounds.assistantHeight.min,
    persistKey: 'upper' }, storage)
  try {
    await pane.resize(1200, 800.25)
    assert.equal(800.25 - pane.currentSize.value, 420)
    pane.keydown({ key: 'End', preventDefault() {} })
    assert.equal(800.25 - pane.currentSize.value, 200)
    pane.keydown({ key: 'Home', preventDefault() {} })
    assert.equal(800.25 - pane.currentSize.value, 420)
  } finally {
    pane.dispose()
  }
})

test('same-axis nested project dividers explicitly transfer size to their adjacent environment pane', () => {
  const source = fs.readFileSync(path.join(__dirname, '../src/views/ProjectsView.vue'), 'utf8')
  assert.match(source, /@resize="resizeProjectNeighbor"/)
  assert.match(source, /function resizeProjectNeighbor\([\s\S]*?allocatedEnvironment\(remaining \+ delta\) - delta/)
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
  // 拖动期两个类都要清掉：split-dragging 负责锁光标与禁选，垂直方向另有自己的类。
  assert.match(end, /classList\.remove\('split-dragging', 'split-dragging-vertical'\)/)
  assert.match(end, /sessionCleanup\?\.\(\)/)
  // 模板上不能再挂 move/up（会被 capture 重定向吞掉）。
  const template = source.slice(source.indexOf('<template>'))
  assert.ok(!/@pointermove|@pointerup/.test(template), 'session events must live on window, not the handle')
})

test('the separator uses outlined resize cursors and a plain line without arrow artwork', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/components/ResizableSplit.vue'), 'utf8')

  // 光标负责方向提示；分隔线不能残留SVG箭头或已不用的图案样式。
  // 光标改用内联 SVG 自定义图（白箭头 + 黑描边），关键字只作回退。
  assert.match(source, /col-resize/)
  assert.match(source, /row-resize/)
  assert.match(source, /url\("data:image\/svg\+xml,/)
  assert.match(source, /stroke='%23000'/, 'cursor artwork must carry a black outline')
  assert.match(source, /fill='%23fff'/, 'cursor artwork must stay light inside the outline')
  assert.ok(!/grip-arrow|data:image\/svg/.test(source.replace(/cursor:\s*\n?\s*url\("data:image\/svg\+xml,[^"]+"\)/g, '')),
    'the separator must not use arrow artwork beyond the cursor image')
  assert.match(source, /\.split-handle:hover > \.split-grip/)

  // 静止时也能看见分割线，悬停和拖动会进一步强化。
  assert.match(source, /\.split-horizontal > \.split-handle > \.split-grip \{[\s\S]*?width: 2px;/)
  assert.match(source, /\.split-vertical > \.split-handle > \.split-grip \{[\s\S]*?height: 2px;/)
  assert.match(source, /background: var\(--border-strong\)/)
  assert.match(source, /opacity: 0\.9/)

  // 悬浮只加粗线条本身，不再给命中带铺一条宽的浅紫底。
  assert.ok(!/\.split-handle:hover::before/.test(source),
    'hover must not paint a wide background band over the hit area')
  assert.ok(!/\.split-handle:focus-visible::before/.test(source))

  // 不用抓手光标。
  assert.ok(!/cursor: (grab|grabbing)/.test(source), 'must not use grab cursors')
})

test('the resize cursor stays locked for the whole drag, not just over the handle', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../src/components/ResizableSplit.vue'), 'utf8')

  // 回归来源：命中带只有十几像素宽，指针拖出该区域后浏览器把光标退回普通箭头，
  // 用户失去“仍在拖动”的反馈。修复约定：拖动期在 body 上强制整页同一光标。
  // 光标是自定义 SVG（无描边的系统关键字对比太弱），关键字只作回退。
  const draggingRule = source.slice(source.indexOf('body.split-dragging),'))
  assert.match(draggingRule, /col-resize !important/, 'drag must lock the horizontal cursor page-wide')
  assert.match(draggingRule, /row-resize !important/, 'vertical splits need the row-resize variant')
  assert.match(draggingRule, /url\("data:image\/svg\+xml,/,
    'the locked cursor must reuse the outlined artwork, not the bare keyword')

  // 只写 body 不够：子元素自身的 cursor（如 button 的 pointer）优先级更高，
  // 必须同时覆盖后代，否则指针停在按钮上仍会变回手型。
  assert.match(source, /body\.split-dragging \*\)[\s\S]{0,400}col-resize !important/)

  // 垂直方向靠独立类区分，且拖动开始时按方向挂上。
  assert.match(source, /split-dragging-vertical/)
  const start = source.slice(source.indexOf('holdTimer = window.setTimeout'), source.indexOf('function keydown('))
  assert.match(start, /props\.direction !== 'horizontal'/)
})


test('the three test pages use the shared history, editor, results and assistant layout', () => {
  for (const view of ['RequestConsole', 'LongConnection', 'TestCasesView']) {
    const source = fs.readFileSync(path.join(__dirname, `../src/views/${view}.vue`), 'utf8')
    assert.match(source, /<TestWorkspace[^>]*history-key="[^"]+"[^>]*editor-key="[^"]+"[^>]*assistant-key="[^"]+"/)
    for (const slot of ['history', 'editor', 'results', 'assistant']) assert.ok(source.includes(`<template #${slot}>`))
    assert.doesNotMatch(source, /<ResizableSplit/)
  }
  const source = fs.readFileSync(path.join(__dirname, '../src/components/TestWorkspace.vue'), 'utf8')
  assert.match(source, /direction="vertical"[\s\S]*?:max-other="bounds.assistantHeight.max"[\s\S]*?#first[\s\S]*?slot name="editor"[\s\S]*?slot name="results"[\s\S]*?slot name="assistant"/)
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
