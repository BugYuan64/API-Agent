<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AutoPane from './AutoPane.vue'
import ManualPane from './ManualPane.vue'
import HistoryList, { type HistoryEntry } from '@/components/HistoryList.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'

/** 用例测试外壳：手动 / 自动是同一功能的两种模式。
 *
 *  结构约束（见「开发实施契约」第 19 节）：
 *  - 页面只有一个「左历史 + 右功能区」工作台，历史列**只渲染一次**；
 *  - 切换模式只替换右侧内容，历史与分栏宽度因此在两种模式间天然共享；
 *  - 两个面板是独立组件、独立页面状态，用 v-show 保留各自输入；
 *  - 模式写入 URL query（?mode=manual|auto），便于分享与刷新还原。
 *
 *  历史数据：两种模式的记录来自不同后端来源（手动写 executions 表，
 *  自动写 JSON 工件），因此由各自面板把自己的记录上报给外壳；
 *  外壳只保留一份列表，供唯一的 `HistoryList` 渲染。 */

type Mode = 'manual' | 'auto'
const MODES: { id: Mode; label: string }[] = [
  { id: 'manual', label: '手动' },
  { id: 'auto', label: '自动' },
]

const route = useRoute()
const router = useRouter()

function readMode(): Mode {
  return route.query.mode === 'auto' ? 'auto' : 'manual'
}

const mode = ref<Mode>(readMode())

watch(
  () => route.query.mode,
  () => {
    mode.value = readMode()
  },
)

function switchTo(next: Mode) {
  if (next === mode.value) return
  mode.value = next
  router.replace({ query: { ...route.query, mode: next } })
}

const hint = computed(() =>
  mode.value === 'manual'
    ? '不调用模型：你写计划，系统只负责执行与断言，结果完全可复现。'
    : '会调用大模型：由文档生成用例并多轮补测。期望可能是推断而非契约，失败不等于目标缺陷。',
)

/* ---------- 唯一的历史：两种模式写入同一个列表 ---------- */

/**
 * 两个面板各自上报自己的运行记录，外壳把它们合并成**一份**按时间倒序的列表。
 * 这样切换模式不会换掉历史内容，标题、空状态、宽度、滚动位置全都只有一份，
 * 不会再出现「一个叫历史执行、一个叫历史运行」的分裂。
 * 每条条目带 `source`，点击时据此分派回对应面板恢复现场。
 */
const manualEntries = ref<HistoryEntry[]>([])
const autoEntries = ref<HistoryEntry[]>([])

const historyEntries = computed<HistoryEntry[]>(() =>
  [...manualEntries.value, ...autoEntries.value]
    .sort((a, b) => (b.at ?? '').localeCompare(a.at ?? '')),
)

/** 当前高亮：取当前模式面板的实际选中项。 */
const activeHistoryId = ref<string | null>(null)

interface PaneHandle { openHistory(id: string): void }
const manualPaneRef = ref<PaneHandle | null>(null)
const autoPaneRef = ref<PaneHandle | null>(null)

function selectHistory(id: string) {
  const entry = historyEntries.value.find((item) => item.id === id)
  if (!entry) return
  if (entry.source === 'auto') autoPaneRef.value?.openHistory(id)
  else manualPaneRef.value?.openHistory(id)
}

/** 面板上报选中项时，仅当它属于当前模式才更新高亮，避免互相覆盖。 */
function trackActive(source: 'manual' | 'auto', id: string | null) {
  if (mode.value === source) activeHistoryId.value = id
}

watch(mode, () => {
  // 切换模式后高亮跟随当前面板已有的选中项。
  activeHistoryId.value = null
})
</script>

<template>
  <TestWorkspace persist-key="api-agent.layout.cases.history" class="testcases">
    <!-- 左：唯一的历史列，两种模式共用（宽度与滚动位置都共享） -->
    <template #history>
      <HistoryList
        title="测试历史"
        :items="historyEntries"
        :active-id="activeHistoryId"
        empty-text="还没有测试记录。手动执行计划或开始一次自动测试后，记录会出现在这里。"
        @select="selectHistory"
      />
    </template>

    <div class="modes">
      <div class="pane" v-show="mode === 'manual'">
        <ManualPane
          ref="manualPaneRef"
          @history="manualEntries = $event"
          @active="trackActive('manual', $event)"
        >
          <template #mode-switch>
            <div class="mode-bar">
              <div class="switcher" role="tablist" aria-label="用例测试模式">
                <button
                  v-for="item in MODES"
                  :key="item.id"
                  role="tab"
                  class="pill"
                  :class="{ 'is-active': mode === item.id }"
                  :aria-selected="mode === item.id"
                  @click="switchTo(item.id)"
                >
                  {{ item.label }}
                </button>
              </div>
              <p class="mode-hint">{{ hint }}</p>
            </div>
          </template>
        </ManualPane>
      </div>

      <div class="pane" v-show="mode === 'auto'">
        <AutoPane
          ref="autoPaneRef"
          @history="autoEntries = $event"
          @active="trackActive('auto', $event)"
        >
          <template #mode-switch>
            <div class="mode-bar">
              <div class="switcher" role="tablist" aria-label="用例测试模式">
                <button
                  v-for="item in MODES"
                  :key="item.id"
                  role="tab"
                  class="pill"
                  :class="{ 'is-active': mode === item.id }"
                  :aria-selected="mode === item.id"
                  @click="switchTo(item.id)"
                >
                  {{ item.label }}
                </button>
              </div>
              <p class="mode-hint">{{ hint }}</p>
            </div>
          </template>
        </AutoPane>
      </div>
    </div>
  </TestWorkspace>
</template>

<style scoped>
.testcases {
  height: 100%;
  min-height: 0;
}

.modes {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.pane {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.pane :deep(.agent-page) {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  padding: 0;
  box-sizing: border-box;
}

/* 模式栏：位于右栏内容区顶部，与另两页的功能区头部同高 */
.mode-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: none;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}

.switcher {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border-radius: 999px;
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  flex: none;
}

.pill {
  padding: 4px 16px;
  border: none;
  border-radius: 999px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12.5px;
  font-weight: 560;
  font-family: inherit;
  cursor: pointer;
  transition: background 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
}

.pill:hover:not(.is-active) {
  color: var(--text-primary);
}

.pill.is-active {
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-sm);
}

.mode-hint {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 1100px) {
  .mode-hint {
    display: none;
  }
}
</style>
