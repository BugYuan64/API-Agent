<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { CircleStop, Loader2, Play, RefreshCw, Waypoints } from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import { cancelRun, createRun, listProjects, listRuns } from '@/api/endpoints'
import type { Project, Run } from '@/api/types'
import { describeRunStatus, formatRelative } from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

/** 模拟任务：阶段 1 的本地确定性执行器。
 *  它不访问目标也不调用模型，界面上明确说明这一点，避免被误读为真实测试结果。 */
const runs = ref<Run[]>([])
const projects = ref<Project[]>([])
const loading = ref(false)
const creating = ref(false)

const form = reactive({
  projectId: '',
  actions: 5,
  durationMs: 120,
  outcome: 'completed' as 'completed' | 'failed',
  seed: 7,
})

// 轮询：有在跑的任务时每 1.5 秒刷新一次，全部结束后自动停。
let timer: number | undefined

const activeCount = computed(
  () => runs.value.filter((run) => ['queued', 'running', 'cancelling'].includes(run.status)).length,
)

const toneOf = (status: string) => {
  if (status === 'completed') return 'success'
  if (status === 'running') return 'info'
  if (status === 'cancelling' || status === 'queued') return 'warning'
  if (status === 'failed') return 'danger'
  return 'neutral'
}

async function load() {
  loading.value = true
  try {
    const page = await listRuns(50)
    runs.value = page.items
  } catch (error) {
    useToastStore().push('danger', '加载任务失败', describeError(error))
  } finally {
    loading.value = false
  }
  schedule()
}

function schedule() {
  window.clearTimeout(timer)
  if (activeCount.value > 0) {
    timer = window.setTimeout(load, 1500)
  }
}

async function loadProjects() {
  try {
    const page = await listProjects(50)
    projects.value = page.items
    if (!form.projectId && page.items.length) form.projectId = page.items[0].id
  } catch {
    projects.value = []
  }
}

async function start() {
  if (!form.projectId) {
    useToastStore().push('warning', '请先创建并选择一个项目')
    return
  }
  creating.value = true
  try {
    const actions = Array.from({ length: form.actions }, (_, index) => ({
      name: `step-${index + 1}`,
      duration_ms: form.durationMs,
      outcome: form.outcome,
    }))
    await createRun({
      project_id: form.projectId,
      actions,
      resource_limits: { max_actions: form.actions, work_timeout_ms: 60_000 },
      random_seed: form.seed,
    })
    useToastStore().push('success', '任务已受理', '模拟动作不发起真实请求')
    await load()
  } catch (error) {
    useToastStore().push('danger', '创建任务失败', describeError(error))
  } finally {
    creating.value = false
  }
}

async function cancel(run: Run) {
  try {
    await cancelRun(run.id)
    await load()
  } catch (error) {
    useToastStore().push('danger', '取消失败', describeError(error))
  }
}

onMounted(async () => {
  await Promise.all([loadProjects(), load()])
})

onBeforeUnmount(() => window.clearTimeout(timer))
</script>

<template>
  <div class="runs">
    <section class="create-card">
      <div class="create-head">
        <div>
          <h2 class="title">模拟任务</h2>
          <p class="subtitle">
            阶段 1 的本地确定性执行器：只 sleep 与记账，不访问目标、不调用模型。
            真实 API 测试请用「用例计划」经由 Postman CLI 执行。
          </p>
        </div>
        <button class="btn btn-sm" :disabled="loading" @click="load">
          <RefreshCw :size="13" :class="{ spinning: loading }" />
          刷新
        </button>
      </div>

      <div class="create-form">
        <label class="field">
          <span class="field-label">项目</span>
          <select v-model="form.projectId" class="select">
            <option value="" disabled>选择项目</option>
            <option v-for="project in projects" :key="project.id" :value="project.id">
              {{ project.name }}
            </option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">动作数</span>
          <input v-model.number="form.actions" class="input" type="number" min="1" max="200" />
        </label>
        <label class="field">
          <span class="field-label">单步耗时（ms）</span>
          <input v-model.number="form.durationMs" class="input" type="number" min="0" max="10000" step="20" />
        </label>
        <label class="field">
          <span class="field-label">结局</span>
          <select v-model="form.outcome" class="select">
            <option value="completed">全部完成</option>
            <option value="failed">制造一次失败</option>
          </select>
        </label>
        <button class="btn btn-accent start-btn" :disabled="creating" @click="start">
          <Loader2 v-if="creating" :size="14" class="spinning" />
          <Play v-else :size="14" />
          创建任务
        </button>
      </div>
    </section>

    <section class="list-card">
      <div class="list-head">
        <span class="list-title">任务列表</span>
        <span v-if="activeCount" class="tag tag-info">{{ activeCount }} 个进行中</span>
      </div>

      <table v-if="runs.length" class="table">
        <thead>
          <tr>
            <th>状态</th>
            <th>进度</th>
            <th>结束原因</th>
            <th>创建</th>
            <th>开始 / 结束</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.id">
            <td>
              <span class="tag" :class="`tag-${toneOf(run.status)}`">
                {{ describeRunStatus(run.status) }}
              </span>
            </td>
            <td class="mono">
              {{ run.current_action }} / {{ run.action_count }}
              <div class="progress">
                <div
                  class="progress-bar"
                  :style="{
                    width: run.action_count
                      ? `${Math.min(100, (run.current_action / run.action_count) * 100)}%`
                      : '0%',
                  }"
                />
              </div>
            </td>
            <td>
              <span class="hint">{{ run.stop_reason ?? '—' }}</span>
              <div v-if="run.workflow_error" class="error-text">{{ run.workflow_error }}</div>
            </td>
            <td class="hint">{{ formatRelative(run.created_at) }}</td>
            <td class="hint">
              {{ formatRelative(run.started_at) }}
              <br />
              {{ run.ended_at ? formatRelative(run.ended_at) : '—' }}
            </td>
            <td class="actions-cell">
              <RouterLink :to="`/runs/${run.id}`" class="btn btn-ghost btn-sm">详情</RouterLink>
              <button
                v-if="['queued', 'running'].includes(run.status)"
                class="btn btn-ghost btn-sm cancel-btn"
                @click="cancel(run)"
              >
                <CircleStop :size="12" />
                取消
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <EmptyState
        v-else
        :icon="Waypoints"
        title="还没有任务"
        description="创建一次模拟任务后，可以在详情页看到事件时间线和预算账本。"
      />
    </section>
  </div>
</template>

<style scoped>
.runs {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  height: 100%;
}

.create-card,
.list-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  padding: 14px 16px;
}

.create-card {
  flex: none;
}

.create-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
}

.subtitle {
  margin: 3px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  max-width: 74ch;
  line-height: 1.7;
}

.create-form {
  display: grid;
  grid-template-columns: 1.4fr 0.7fr 0.9fr 0.9fr auto;
  gap: 10px;
  align-items: end;
}

.start-btn {
  height: 32px;
}

.list-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.list-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.list-title {
  font-size: 12.5px;
  font-weight: 600;
}

.table {
  overflow: auto;
}

.progress {
  margin-top: 4px;
  height: 3px;
  width: 92px;
  border-radius: 99px;
  background: var(--bg-sunken);
  overflow: hidden;
}

.progress-bar {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

.error-text {
  font-size: 10.5px;
  color: var(--danger);
}

.actions-cell {
  display: flex;
  gap: 4px;
  white-space: nowrap;
}

.cancel-btn {
  color: var(--danger);
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1080px) {
  .create-form {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
