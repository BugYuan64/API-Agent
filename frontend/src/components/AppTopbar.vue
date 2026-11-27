<script setup lang="ts">
/** 应用顶栏：依赖健康检查 API、引擎共享选择和图标组件，分别显示管理后端及执行引擎状态。探测失败显示未知或异常状态，不替代各页面的业务错误提示。 */

import { computed, onMounted, ref, watch } from 'vue'
import { Activity, ChevronRight, RefreshCw, ServerCog, Settings as SettingsIcon } from 'lucide-vue-next'
import { engineStatus, getHealth } from '@/api/endpoints'
import type { EngineStatus } from '@/api/types'
import { engineMeta } from '@/lib/engines'
import { useEngineSelection } from '@/lib/engines'

/** 顶栏展示两个真实状态：管理后端是否存活、执行引擎是否可用。
 *  两者都失败时界面仍可用（页面会各自提示），但用户能立刻知道问题出在哪一层。 */
const healthError = ref(false)
const engine = ref<EngineStatus | null>(null)
const loading = ref(false)
let engineProbeSeq = 0

/** 顶栏显示“当前选中的引擎”及其状态，而不是写死某一个引擎名称。 */
const { selected: selectedEngine, refresh: refreshEngineList } = useEngineSelection()

/** 仅按当前选中引擎重新探测状态与版本号，并用单调序号屏蔽乱序返回。 */
async function refreshSelectedEngine(target: string = selectedEngine.value) {
  const seq = ++engineProbeSeq
  engine.value = null
  try {
    const result = await engineStatus(target)
    if (seq === engineProbeSeq && selectedEngine.value === target) {
      engine.value = result
    }
  } catch {
    if (seq === engineProbeSeq && selectedEngine.value === target) {
      engine.value = null
    }
  }
}

/** 刷新引擎列表后并行探测管理后端和所选引擎；探测失败各自映射为异常或未知。列表初始化错误向上传播，但始终解除 loading。 */
async function refresh() {
  loading.value = true
  try {
    // 首次显示时先确保引擎列表（含服务端默认值）已就绪，再按选中项探活，
    // 否则顶栏会一直停在本地默认值上。
    if (!engine.value) await refreshEngineList()
    const target = selectedEngine.value
    const seq = ++engineProbeSeq
    const [healthResult, engineResult] = await Promise.allSettled([
      getHealth(),
      engineStatus(target),
    ])
    healthError.value = healthResult.status === 'rejected'
    if (seq === engineProbeSeq && selectedEngine.value === target) {
      engine.value = engineResult.status === 'fulfilled' ? engineResult.value : null
    }
  } finally {
    loading.value = false
  }
}

watch(selectedEngine, (next, prev) => {
  if (next !== prev) {
    void refreshSelectedEngine(next)
  }
})

/** 结合当前引擎元信息与探测结果生成名称、不可用或未知标签；只在已有版本时追加版本号。 */
const engineLabel = computed(() => {
  const name = engineMeta(selectedEngine.value).name
  if (!engine.value) return `${name} 状态未知`
  if (!engine.value.available) return `${name} 不可用`
  return engine.value.version ? `${name} v${engine.value.version}` : name
})

/** 将当前引擎探测结果转换为展示色调；没有结果为 neutral，不可用为 warning。 */
const engineTone = computed(() => {
  if (!engine.value) return 'neutral'
  return engine.value.available ? 'success' : 'warning'
})

onMounted(refresh)
</script>

<template>
  <header class="topbar">
    <div class="crumbs">
      <span class="crumb-root">API 工作台</span>
      <ChevronRight class="crumb-sep" :size="13" aria-hidden="true" />
      <span class="crumb-current">{{ $route.meta.title ?? '控制台' }}</span>
    </div>

    <div class="status">
      <div class="pill" :class="healthError ? 'pill-danger' : 'pill-success'" :title="healthError ? '后端未连接' : '后端在线'" :aria-label="healthError ? '后端未连接' : '后端在线'">
        <Activity :size="12" />
        <span>{{ healthError ? '后端未连接' : '后端在线' }}</span>
      </div>
      <div class="pill" :class="`pill-${engineTone}`" :title="engineLabel" :aria-label="engineLabel">
        <ServerCog :size="12" />
        <span>{{ engineLabel }}</span>
      </div>
      <button class="btn btn-ghost btn-icon" title="刷新状态" :disabled="loading" @click="refresh">
        <RefreshCw :size="14" :class="{ spinning: loading }" />
      </button>
      <RouterLink to="/settings" class="btn btn-ghost btn-icon" title="系统设置与大模型配置">
        <SettingsIcon :size="14" />
      </RouterLink>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: var(--topbar-height);
  flex: none;
  padding: 0 18px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  min-width: 0;
}

.crumb-root {
  white-space: nowrap;
  color: var(--text-tertiary);
}

.crumb-sep {
  color: var(--border-strong);
}

.crumb-current {
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.status {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 7px;
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 500;
  border: 1px solid transparent;
}

.pill-success {
  color: var(--success);
}

.pill-warning {
  background: var(--warning-soft);
  color: var(--warning);
  border-color: var(--warning-border);
}

.pill-danger {
  background: var(--danger-soft);
  color: var(--danger);
  border-color: var(--danger-border);
}

.pill-neutral {
  color: var(--text-secondary);
}

.status > .btn-icon { width: 27px; height: 27px; }

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 800px) {
  .topbar { padding: 0 12px; gap: 8px; }
  .crumb-root, .crumb-sep { display: none; }
  .status { gap: 4px; }
  .pill { padding: 0 6px; font-size: 10.5px; }
}

@media (max-width: 600px) {
  .pill span { display: none; }
  .pill { width: 26px; justify-content: center; }
}
</style>
