<script setup lang="ts">
/** 向后兼容别名：统一 URL 输入实现已收敛至 UrlInput.vue。 */
import UrlInput from '@/components/UrlInput.vue'

defineProps<{
  modelValue: string
  address: string
  label: string
  placeholder: string
  websocket?: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [prefix: string]
  'update:address': [address: string]
  submit: []
}>()
</script>

<template>
  <UrlInput
    :model-value="modelValue"
    :address="address"
    :label="label"
    :placeholder="placeholder"
    :websocket="websocket"
    :disabled="disabled"
    @update:model-value="emit('update:modelValue', $event)"
    @update:address="emit('update:address', $event)"
    @submit="emit('submit')"
  >
    <template v-if="$slots.leading" #leading>
      <slot name="leading" />
    </template>
  </UrlInput>
</template>
