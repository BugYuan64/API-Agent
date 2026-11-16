<script setup lang="ts">
/** Iconify wrapper with locally bundled icon data: no runtime icon service requests. */
import { computed } from 'vue'
import { Icon } from '@iconify/vue'
import { workflow, play, download } from '@/lib/icons'
const props = withDefaults(defineProps<{ name: string; size?: number }>(), { size: 16 })
const available = { workflow, play, download }
const data = computed(() => available[props.name as keyof typeof available] ?? workflow)
</script>
<template><Icon :icon="data" :width="size" :height="size" aria-hidden="true" /></template>
