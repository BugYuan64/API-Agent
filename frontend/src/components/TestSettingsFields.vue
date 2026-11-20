<script setup lang="ts">
/** Edit the same persisted settings in the workbench and system settings. */
import { computed } from 'vue'
import { normalizeTestNumber, testNumberFields, testSettings, type TestSettingsGroup } from '@/lib/testSettings'
import { useToastStore } from '@/stores/toasts'

const props = defineProps<{ group: TestSettingsGroup; disabled?: boolean; generationLocked?: boolean }>()
type Field = typeof testNumberFields[TestSettingsGroup][number]
const fields = computed(() => testNumberFields[props.group])
const values = computed(() => testSettings[props.group] as Record<string, number | boolean>)
const toast = useToastStore()

/** Commit the bounded value and explain any correction in the same place. */
function updateNumber(event: Event, field: Field) {
  const input = event.currentTarget as HTMLInputElement
  const { value, adjustment } = normalizeTestNumber(input.valueAsNumber, Number(values.value[field.key]), field.min, field.max)
  input.value = String(value)
  values.value[field.key] = value
  if (adjustment === 'range') toast.push('warning', `${field.label}超出范围`, `允许 ${field.min}–${field.max}，已调整为 ${value}。`)
  else if (adjustment === 'round') toast.push('info', `${field.label}需要整数`, `已调整为 ${value}。`)
  else if (adjustment === 'empty') toast.push('warning', `${field.label}需要有效数字`, `已恢复为 ${value}。`)
}
</script>

<template>
  <div class="test-settings-fields">
    <label v-for="field in fields" :key="field.key" class="number-field">
      <span>{{ field.label }}</span>
      <input class="input" type="number" :min="field.min" :max="field.max" :value="values[field.key]"
        :disabled="disabled || (field.key === 'testsPerUrl' && generationLocked)" @change="updateNumber($event, field)" />
      <span class="hint">{{ field.min }}–{{ field.max }}</span>
    </label>
    <label v-if="group === 'console'" class="check-field">
      <input v-model="testSettings.console.followRedirects" class="switch" type="checkbox" :disabled="disabled" /> 跟随重定向
    </label>
    <label v-if="group === 'task'" class="check-field">
      <input v-model="testSettings.task.allowWrites" type="checkbox" :disabled="disabled" /> 允许写接口和 WebSocket 发送消息
    </label>
  </div>
</template>

<style scoped>
.test-settings-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr)); gap: 10px 12px; }
.number-field { display: grid; gap: 4px; align-content: start; min-width: 0; }
.number-field > .input { width: 100%; }
.check-field { grid-column: 1 / -1; display: flex; gap: 6px; align-items: center; }
</style>
