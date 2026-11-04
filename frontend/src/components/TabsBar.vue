<script setup lang="ts" generic="T extends string">
/**
 * 标签页切换条：受控组件（`v-model`）+ 泛型键，避免调用方到处写类型断言。
 *
 * 作用：历史、请求、任务详情三个页面的 tabs 结构一致但各写一遍，
 * 样式与「选中下划线」也各有一份；调用方还需要 `as typeof tab` 才能通过类型检查。
 * 这里用具名插槽前的泛型参数把「键的类型」交给调用方推断，`update:modelValue`
 * 直接给出正确类型，不再需要断言。
 */
defineProps<{
  items: readonly { key: T; label: string; count?: number }[]
}>()

const model = defineModel<T>({ required: true })
</script>

<template>
  <div class="tabs" role="tablist">
    <button
      v-for="item in items"
      :key="item.key"
      type="button"
      role="tab"
      class="tab"
      :class="{ 'tab-active': item.key === model }"
      :aria-selected="item.key === model"
      @click="model = item.key"
    >
      <span>{{ item.label }}</span>
      <span v-if="item.count !== undefined" class="tab-count">{{ item.count }}</span>
    </button>
  </div>
</template>

<style scoped>
.tab-count {
  min-width: 16px;
  padding: 0 4px;
  border-radius: 99px;
  background: var(--bg-subtle);
  color: var(--text-tertiary);
  font-size: 10px;
  line-height: 15px;
  text-align: center;
}
</style>
