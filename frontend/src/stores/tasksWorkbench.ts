/** 智能群测工作台跨页持久化状态：保存草稿、目录、来源、活跃任务、只读回放与聚焦用例，切页不丢现场。 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { Catalog, TaskDocument, TaskRecord, TaskSnapshot, TestCase } from '@/api/tasks'
import type { ReadonlyReplayState } from './workbenchRuns'

export const useTasksWorkbenchStore = defineStore('tasksWorkbench', () => {
  const origin = ref('')
  const urlPrefix = ref('')
  const catalog = ref<Catalog | null>(null)
  const document = ref<TaskDocument | null>(null)
  const filename = ref('')
  const run = ref<TaskRecord | null>(null)
  const activeTaskId = ref<string | null>(null)
  const replaySource = ref<TaskRecord | null>(null)
  const replayEdits = ref<Record<number, TestCase>>({})
  const resultsCleared = ref(false)
  const draftVersion = ref(0)
  const readonlyReplay = ref<ReadonlyReplayState | null>(null)
  const focusedCaseId = ref<string | null>(null)
  const detachedFromActive = ref(false)

  const replayDraft = computed<TaskSnapshot | null>(() => {
    const snapshot = replaySource.value?.snapshot
    if (!snapshot) return null
    return {
      ...snapshot,
      cases: snapshot.cases.map((input, index) => replayEdits.value[index] ?? input),
    }
  })

  function setRun(record: TaskRecord | null): void {
    run.value = record
    activeTaskId.value = record?.id ?? null
    if (record) detachedFromActive.value = false
  }

  function setReadonlyReplay(state: ReadonlyReplayState | null): void {
    readonlyReplay.value = state ? { ...state } : null
    focusedCaseId.value = state?.caseId ?? null
  }

  function clearReadonlyReplay(): void {
    readonlyReplay.value = null
    focusedCaseId.value = null
  }

  function resetWorkspace(options?: { incrementDraftVersion?: boolean; detached?: boolean }): void {
    origin.value = ''
    urlPrefix.value = ''
    catalog.value = null
    document.value = null
    filename.value = ''
    run.value = null
    activeTaskId.value = null
    replaySource.value = null
    replayEdits.value = {}
    resultsCleared.value = false
    readonlyReplay.value = null
    focusedCaseId.value = null
    if (options?.incrementDraftVersion) draftVersion.value++
    if (typeof options?.detached === 'boolean') detachedFromActive.value = options.detached
  }

  return {
    origin,
    urlPrefix,
    catalog,
    document,
    filename,
    run,
    activeTaskId,
    replaySource,
    replayDraft,
    replayEdits,
    resultsCleared,
    draftVersion,
    readonlyReplay,
    focusedCaseId,
    detachedFromActive,
    setRun,
    setReadonlyReplay,
    clearReadonlyReplay,
    resetWorkspace,
  }
})
