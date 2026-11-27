<script setup lang="ts">
/**
 * 动态信号发射项目 Logo：中心信标持续脉冲并向外发射多圈电磁波，
 * 主色绑定全局调色板 `--logo-color`，支持在设置页实时自定义与重置。
 */
withDefaults(
  defineProps<{
    size?: number
  }>(),
  { size: 32 },
)
</script>

<template>
  <span
    class="signal-logo"
    :style="{ width: `${size}px`, height: `${size}px` }"
    aria-hidden="true"
  >
    <svg viewBox="0 0 36 36" fill="none" class="signal-svg">
      <!-- 持续向外扩散的环形雷达脉冲 -->
      <circle class="signal-ring ring-1" cx="18" cy="18" r="5" />
      <circle class="signal-ring ring-2" cx="18" cy="18" r="5" />

      <!-- 左侧发射信号弧 -->
      <path class="signal-arc arc-inner" d="M13.2 13.4a6.5 6.5 0 0 0 0 9.2" />
      <path class="signal-arc arc-outer" d="M9.6 9.8a11.6 11.6 0 0 0 0 16.4" />

      <!-- 右侧发射信号弧 -->
      <path class="signal-arc arc-inner" d="M22.8 13.4a6.5 6.5 0 0 1 0 9.2" />
      <path class="signal-arc arc-outer" d="M26.4 9.8a11.6 11.6 0 0 1 0 16.4" />

      <!-- 信标底座与中心发射核 -->
      <path class="signal-mast" d="M18 20.4v6.8M14.8 27.2h6.4" />
      <circle class="signal-core-halo" cx="18" cy="18" r="4.1" />
      <circle class="signal-core" cx="18" cy="18" r="2.7" />
    </svg>
  </span>
</template>

<style scoped>
.signal-logo {
  --signal-accent: var(--logo-color, var(--brand-highlight, #7c6cf6));
  position: relative;
  display: inline-grid;
  flex: none;
  place-items: center;
  border-radius: 10px;
  color: var(--signal-accent);
  background:
    radial-gradient(
      circle at 50% 45%,
      color-mix(in srgb, var(--signal-accent) 26%, transparent),
      color-mix(in srgb, var(--signal-accent) 10%, var(--bg-surface)) 72%
    );
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--signal-accent) 42%, transparent),
    0 2px 10px color-mix(in srgb, var(--signal-accent) 22%, transparent);
  transition: transform 240ms cubic-bezier(0.22, 0.75, 0.2, 1), box-shadow 240ms ease;
}

.signal-svg {
  width: 88%;
  height: 88%;
  overflow: visible;
}

.signal-ring {
  stroke: var(--signal-accent);
  stroke-width: 1.4;
  transform-box: fill-box;
  transform-origin: center;
  opacity: 0;
  animation: signal-ring-emit 2.4s cubic-bezier(0.16, 0.84, 0.44, 1) infinite;
}

.ring-2 {
  animation-delay: 1.2s;
}

.signal-arc {
  stroke: var(--signal-accent);
  stroke-linecap: round;
  stroke-width: 2;
  opacity: 0.28;
  animation: signal-arc-wave 1.8s ease-in-out infinite;
}

.arc-inner {
  animation-delay: 0s;
}

.arc-outer {
  stroke-width: 1.7;
  animation-delay: 0.38s;
}

.signal-mast {
  stroke: color-mix(in srgb, var(--signal-accent) 82%, var(--text-primary));
  stroke-width: 1.8;
  stroke-linecap: round;
}

.signal-core-halo {
  fill: color-mix(in srgb, var(--signal-accent) 34%, transparent);
  transform-box: fill-box;
  transform-origin: center;
  animation: signal-core-pulse 1.8s ease-in-out infinite;
}

.signal-core {
  fill: var(--signal-accent);
  stroke: var(--text-on-accent, #ffffff);
  stroke-width: 0.9;
}

@keyframes signal-ring-emit {
  0% {
    transform: scale(0.75);
    opacity: 0.72;
  }
  70% {
    opacity: 0.18;
  }
  100% {
    transform: scale(2.85);
    opacity: 0;
  }
}

@keyframes signal-arc-wave {
  0%, 100% {
    opacity: 0.22;
  }
  42% {
    opacity: 1;
  }
  75% {
    opacity: 0.42;
  }
}

@keyframes signal-core-pulse {
  0%, 100% {
    transform: scale(0.92);
    opacity: 0.45;
  }
  50% {
    transform: scale(1.42);
    opacity: 0.92;
  }
}

/* 保持信号发射动画持续运行，不受系统全局动画截断影响。 */
@media (prefers-reduced-motion: reduce) {
  .signal-ring {
    animation-duration: 2.4s !important;
    animation-iteration-count: infinite !important;
  }
  .signal-arc,
  .signal-core-halo {
    animation-duration: 1.8s !important;
    animation-iteration-count: infinite !important;
  }
}
</style>
