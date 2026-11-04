<script setup lang="ts">
/**
 * 表单字段：标签 + 提示 + 输入控件（默认插槽）。
 *
 * 作用：`<label class="field"><span class="field-label">…</span><input class="input"></label>`
 * 这组标记在 6 个视图里重复了 30 余次，且各视图对 `.field-label` 的局部覆盖互不一致。
 * 收敛后，标签排版、必填星号与提示位置只在一处定义。
 */
defineProps<{
  label: string
  /** 标签右侧的补充说明（如「支持任何兼容 OpenAI 规范的端点」）。 */
  hint?: string
  required?: boolean
  /** 占满整行（两列栅格里使用）。 */
  wide?: boolean
}>()
</script>

<template>
  <label class="form-field" :class="{ wide }">
    <span class="form-field-head">
      <span class="form-field-label">
        {{ label }}
        <span v-if="required" class="form-field-required">*</span>
      </span>
      <span v-if="hint" class="form-field-hint">{{ hint }}</span>
    </span>
    <slot />
  </label>
</template>

<style scoped>
.form-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.form-field.wide {
  grid-column: 1 / -1;
}

.form-field-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.form-field-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.form-field-required {
  color: var(--danger);
}

.form-field-hint {
  font-size: 11px;
  color: var(--text-tertiary);
}
</style>
