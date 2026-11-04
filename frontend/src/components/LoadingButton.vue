<script setup lang="ts">
/**
 * 加载态按钮：`loading` 时显示转圈并禁用，否则显示给定图标 + 文案。
 *
 * 作用：`<Loader2 v-if="x" class="spinning"/><Icon v-else/>` 这组配对在
 * 6 个视图里写了 12 遍，每处还要自己管 disabled 与文案切换。
 * 统一后调用方只关心「是否加载中」与「平时显示什么」。
 */
import { Loader2 } from 'lucide-vue-next'
import type { Component } from 'vue'

const props = withDefaults(
  defineProps<{
    loading?: boolean
    icon?: Component
    label: string
    loadingLabel?: string
    /** 视觉变体，对应 base.css 中的按钮类。 */
    variant?: 'primary' | 'accent' | 'ghost' | 'danger'
    disabled?: boolean
    size?: 'sm' | 'md'
  }>(),
  { loading: false, variant: 'ghost', disabled: false, size: 'md' },
)

const classes = ['btn', `btn-${props.variant}`, props.size === 'sm' ? 'btn-sm' : '']
</script>

<template>
  <button :class="classes" :disabled="loading || disabled" type="button">
    <Loader2 v-if="loading" :size="size === 'sm' ? 13 : 14" class="spinning" />
    <component :is="icon" v-else-if="icon" :size="size === 'sm' ? 13 : 14" />
    <span>{{ loading ? (loadingLabel ?? label) : label }}</span>
  </button>
</template>
