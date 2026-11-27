<script setup lang="ts">
/**
 * AI 助手小Y 动态多表情机器人图标：
 * 仅保留发光 LED 风格的机器人眼睛与嘴巴（无头部轮廓），内置 8 种机械表情随机切换，默认呈现绿色，配色受全局 `--assistant-color` 控制。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

type RobotExpression =
  | 'happy'
  | 'wink'
  | 'blink'
  | 'sparkle'
  | 'curious'
  | 'cool'
  | 'surprised'
  | 'sleepy'

const EXPRESSIONS: readonly RobotExpression[] = [
  'happy',
  'wink',
  'blink',
  'sparkle',
  'curious',
  'cool',
  'surprised',
  'sleepy',
]

const props = withDefaults(
  defineProps<{
    size?: number
    state?: 'idle' | 'thinking' | 'reading'
    minIntervalMs?: number
    maxIntervalMs?: number
  }>(),
  {
    size: 30,
    state: 'idle',
    minIntervalMs: 2600,
    maxIntervalMs: 4200,
  },
)

const expression = ref<RobotExpression>('happy')
let timer: number | undefined

/** 随机切换到与当前不同的下一种机器人表情。 */
function nextRandomExpression(): RobotExpression {
  const candidates = EXPRESSIONS.filter((item) => item !== expression.value)
  const chosen = candidates[Math.floor(Math.random() * candidates.length)] ?? 'happy'
  expression.value = chosen
  return chosen
}

function scheduleNext(): void {
  const span = Math.max(0, props.maxIntervalMs - props.minIntervalMs)
  const delay = props.minIntervalMs + Math.floor(Math.random() * (span + 1))
  timer = window.setTimeout(() => {
    nextRandomExpression()
    scheduleNext()
  }, delay)
}

function cycleNow(): void {
  window.clearTimeout(timer)
  nextRandomExpression()
  scheduleNext()
}

onMounted(() => {
  scheduleNext()
})

onBeforeUnmount(() => {
  window.clearTimeout(timer)
})

defineExpose({ expression, nextRandomExpression, EXPRESSIONS })
</script>

<template>
  <span
    class="assistant-avatar"
    :class="[`expr-${expression}`, `state-${props.state}`]"
    :style="{ width: `${props.size}px`, height: `${props.size}px` }"
    :data-expression="expression"
    title="点击切换小Y表情"
    aria-hidden="true"
    @click="cycleNow"
  >
    <svg viewBox="0 0 36 36" fill="none" class="face-svg">
      <!-- 表情 1: 开心机甲折线弯眼 + 倒角电子微笑 -->
      <g v-if="expression === 'happy'" class="face-group face-happy">
        <path class="face-stroke" d="M5.6 16.0 L9.8 11.4 L14.0 16.0" />
        <path class="face-stroke" d="M22.0 16.0 L26.2 11.4 L30.4 16.0" />
        <path class="face-mouth" d="M11.6 22.8 L14.4 25.8 H21.6 L24.4 22.8" />
      </g>

      <!-- 表情 2: 发光 LED 像素方眼 + 机械 Wink -->
      <g v-else-if="expression === 'wink'" class="face-group face-wink">
        <rect class="face-led-eye" x="5.8" y="10.2" width="8.8" height="8.6" rx="2.4" />
        <rect class="face-led-pixel" x="10.8" y="11.7" width="2.2" height="2.2" rx="0.6" />
        <path class="face-stroke" d="M22.6 11.6 L29.6 14.5 L22.6 17.4" />
        <path class="face-mouth" d="M12.0 23.2 L15.0 25.8 H21.6 L24.6 22.4" />
      </g>

      <!-- 表情 3: 双屏 LED 方眼眨眼 + 声波格嘴 -->
      <g v-else-if="expression === 'blink'" class="face-group face-blink">
        <g class="eye-blinking">
          <rect class="face-led-eye" x="5.8" y="10.2" width="8.8" height="8.6" rx="2.4" />
          <rect class="face-led-pixel" x="10.8" y="11.7" width="2.2" height="2.2" rx="0.6" />
        </g>
        <g class="eye-blinking">
          <rect class="face-led-eye" x="21.4" y="10.2" width="8.8" height="8.6" rx="2.4" />
          <rect class="face-led-pixel" x="26.4" y="11.7" width="2.2" height="2.2" rx="0.6" />
        </g>
        <rect class="face-eq-bar eq-1" x="11.6" y="23.4" width="3.2" height="2.4" rx="0.8" />
        <rect class="face-eq-bar eq-2" x="16.4" y="22.8" width="3.2" height="3.6" rx="0.8" />
        <rect class="face-eq-bar eq-3" x="21.2" y="23.4" width="3.2" height="2.4" rx="0.8" />
      </g>

      <!-- 表情 4: 像素星芒眼 + 发光梯形格嘴 -->
      <g v-else-if="expression === 'sparkle'" class="face-group face-sparkle">
        <path class="face-star" d="M10.2 9.4 L11.7 13.0 L15.3 14.5 L11.7 16.0 L10.2 19.6 L8.7 16.0 L5.1 14.5 L8.7 13.0 Z" />
        <path class="face-star" d="M25.8 9.4 L27.3 13.0 L30.9 14.5 L27.3 16.0 L25.8 19.6 L24.3 16.0 L20.7 14.5 L24.3 13.0 Z" />
        <path class="face-mouth-box" d="M11.6 22.4 H24.4 L22.2 26.2 H13.8 Z" />
      </g>

      <!-- 表情 5: 双雷达扫描方眼 + 数字示波脉冲嘴 -->
      <g v-else-if="expression === 'curious'" class="face-group face-curious">
        <rect class="face-led-frame" x="5.8" y="10.6" width="8.8" height="7.6" rx="2.0" />
        <rect class="face-led-frame" x="21.4" y="10.6" width="8.8" height="7.6" rx="2.0" />
        <g class="eye-scan">
          <rect class="face-led-eye" x="8.8" y="12.6" width="2.8" height="3.6" rx="0.8" />
          <rect class="face-led-eye" x="24.4" y="12.6" width="2.8" height="3.6" rx="0.8" />
        </g>
        <path class="face-mouth" d="M11.2 24.2 H14.4 L16.2 21.8 L19.8 26.2 L21.6 24.2 H24.8" />
      </g>

      <!-- 表情 6: 赛博双光条眼 + 电子平条嘴 -->
      <g v-else-if="expression === 'cool'" class="face-group face-cool">
        <rect class="face-led-frame" x="4.8" y="12.2" width="11.0" height="4.6" rx="1.6" />
        <rect class="face-cool-glint" x="5.8" y="13.3" width="3.4" height="2.4" rx="0.8" />
        <rect class="face-led-frame" x="20.2" y="12.2" width="11.0" height="4.6" rx="1.6" />
        <rect class="face-cool-glint" x="21.2" y="13.3" width="3.4" height="2.4" rx="0.8" />
        <rect class="face-eq-bar" x="12.4" y="23.6" width="11.2" height="2.4" rx="0.9" />
      </g>

      <!-- 表情 7: 警报方框眼 + 电子方 O 嘴 -->
      <g v-else-if="expression === 'surprised'" class="face-group face-surprised">
        <rect class="face-led-frame" x="5.4" y="9.8" width="9.4" height="9.2" rx="2.4" />
        <rect class="face-led-eye" x="8.5" y="12.8" width="3.2" height="3.2" rx="0.8" />
        <rect class="face-led-frame" x="21.2" y="9.8" width="9.4" height="9.2" rx="2.4" />
        <rect class="face-led-eye" x="24.3" y="12.8" width="3.2" height="3.2" rx="0.8" />
        <rect class="face-mouth-box" x="14.6" y="21.8" width="6.8" height="5.2" rx="1.6" />
      </g>

      <!-- 表情 8: 低功耗待机窄缝眼 + 呼吸横杠嘴 -->
      <g v-else class="face-group face-sleepy">
        <rect class="face-led-eye face-dim" x="5.6" y="13.6" width="9.0" height="2.6" rx="1.0" />
        <rect class="face-led-eye face-dim" x="21.4" y="13.6" width="9.0" height="2.6" rx="1.0" />
        <rect class="face-eq-bar face-standby" x="14.4" y="23.6" width="7.2" height="2.2" rx="0.9" />
      </g>
    </svg>
  </span>
</template>

<style scoped>
.assistant-avatar {
  --bot-color: var(--assistant-color, #22c55e);
  display: inline-grid;
  flex: none;
  place-items: center;
  cursor: pointer;
  color: var(--bot-color);
  user-select: none;
  transition: transform 200ms ease, filter 200ms ease;
}

.assistant-avatar:hover {
  transform: scale(1.08);
}

.face-svg {
  width: 100%;
  height: 100%;
  overflow: visible;
  filter: drop-shadow(0 0 2.2px color-mix(in srgb, var(--bot-color) 42%, transparent));
  animation: face-float 2.8s ease-in-out infinite;
}

.state-thinking .face-svg,
.state-reading .face-svg {
  animation-duration: 1.15s;
  filter: drop-shadow(0 0 3.4px color-mix(in srgb, var(--bot-color) 62%, transparent));
}

.face-led-eye,
.face-star,
.face-eq-bar {
  fill: var(--bot-color);
}

.face-led-pixel {
  fill: var(--bg-surface, #ffffff);
  opacity: 0.92;
}

.face-led-frame {
  fill: color-mix(in srgb, var(--bot-color) 18%, transparent);
  stroke: var(--bot-color);
  stroke-width: 2.1;
}

.face-stroke,
.face-mouth {
  stroke: var(--bot-color);
  stroke-width: 2.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.face-mouth {
  stroke-width: 2.4;
}

.face-mouth-box {
  fill: color-mix(in srgb, var(--bot-color) 20%, transparent);
  stroke: var(--bot-color);
  stroke-width: 2.2;
  stroke-linejoin: round;
}

.face-dim {
  opacity: 0.82;
}

.face-standby {
  animation: face-standby-pulse 2.2s ease-in-out infinite;
}

.eye-blinking {
  transform-box: fill-box;
  transform-origin: center;
  animation: face-blink 2.2s ease-in-out infinite;
}

.eq-1,
.eq-2,
.eq-3 {
  transform-box: fill-box;
  transform-origin: center;
  animation: face-eq 1.4s ease-in-out infinite;
}

.eq-2 {
  animation-delay: 0.18s;
}

.eq-3 {
  animation-delay: 0.36s;
}

.face-sparkle .face-star {
  transform-box: fill-box;
  transform-origin: center;
  animation: face-twinkle 1.1s ease-in-out infinite;
}

.eye-scan {
  animation: face-look 1.9s ease-in-out infinite;
}

.face-cool-glint {
  fill: var(--bot-color);
  animation: face-scan 1.35s ease-in-out infinite;
}

@keyframes face-float {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-1.2px);
  }
}

@keyframes face-blink {
  0%, 42%, 52%, 100% {
    transform: scaleY(1);
  }
  47% {
    transform: scaleY(0.14);
  }
}

@keyframes face-eq {
  0%, 100% {
    transform: scaleY(0.85);
  }
  50% {
    transform: scaleY(1.22);
  }
}

@keyframes face-standby-pulse {
  0%, 100% {
    opacity: 0.45;
  }
  50% {
    opacity: 0.95;
  }
}

@keyframes face-twinkle {
  0%, 100% {
    transform: scale(0.86) rotate(0deg);
    opacity: 0.84;
  }
  50% {
    transform: scale(1.14) rotate(12deg);
    opacity: 1;
  }
}

@keyframes face-look {
  0%, 100% {
    transform: translateX(0);
  }
  30% {
    transform: translateX(-1.8px);
  }
  70% {
    transform: translateX(1.8px);
  }
}

@keyframes face-scan {
  0%, 100% {
    transform: translateX(0);
  }
  50% {
    transform: translateX(5.4px);
  }
}

/* 保留小Y机械 LED 表情微动效。 */
@media (prefers-reduced-motion: reduce) {
  .face-svg {
    animation-duration: 2.8s !important;
    animation-iteration-count: infinite !important;
  }
  .eye-blinking {
    animation-duration: 2.2s !important;
    animation-iteration-count: infinite !important;
  }
  .eq-1,
  .eq-2,
  .eq-3 {
    animation-duration: 1.4s !important;
    animation-iteration-count: infinite !important;
  }
  .face-standby {
    animation-duration: 2.2s !important;
    animation-iteration-count: infinite !important;
  }
  .face-sparkle .face-star {
    animation-duration: 1.1s !important;
    animation-iteration-count: infinite !important;
  }
  .eye-scan {
    animation-duration: 1.9s !important;
    animation-iteration-count: infinite !important;
  }
  .face-cool-glint {
    animation-duration: 1.35s !important;
    animation-iteration-count: infinite !important;
  }
}
</style>
