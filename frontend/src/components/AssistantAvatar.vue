<script setup lang="ts">
/**
 * AI 助手小Y 动态多表情头像：
 * 复刻圆角蓝方块本体与纯白胶囊双眼的极简造型（无嘴部元素），
 * 内置 11 种纯眼部动态表情，按定时器自动随机切换，支持点击即时切换，
 * 脸部底色默认 `#3c82f6`（受 `--assistant-color` 控制），表情眼色默认 `#ffffff`（受 `--assistant-eye-color` 控制）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

type RobotExpression =
  | 'peek'
  | 'happy'
  | 'wink'
  | 'look'
  | 'blink'
  | 'sparkle'
  | 'curious'
  | 'focused'
  | 'surprised'
  | 'sleepy'
  | 'love'

const EXPRESSIONS: readonly RobotExpression[] = [
  'peek',
  'happy',
  'wink',
  'look',
  'blink',
  'sparkle',
  'curious',
  'focused',
  'surprised',
  'sleepy',
  'love',
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

const expression = ref<RobotExpression>('peek')
let timer: number | undefined

/** 随机切换到与当前不同的下一种纯眼部表情。 */
function nextRandomExpression(): RobotExpression {
  const candidates = EXPRESSIONS.filter((item) => item !== expression.value)
  const chosen = candidates[Math.floor(Math.random() * candidates.length)] ?? 'peek'
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
      <g class="avatar-shell">
        <!-- 圆角方块本体（贴合参考图比例与圆角，配色跟随 --assistant-color） -->
        <rect class="avatar-body" x="2" y="2" width="32" height="32" rx="11.5" />

        <!-- 表情 1: 招牌右看胶囊双眼（默认态，与参考图一致） -->
        <g v-if="expression === 'peek'" key="peek" class="eyes-group eyes-peek">
          <g class="peek-gaze">
            <rect class="eye-fill eye-peek-blink" x="13.6" y="11.6" width="5.4" height="9.6" rx="2.7" />
            <rect class="eye-fill eye-peek-blink" x="23.0" y="11.6" width="5.4" height="9.6" rx="2.7" />
          </g>
        </g>

        <!-- 表情 2: 开心弯月眼 -->
        <g v-else-if="expression === 'happy'" key="happy" class="eyes-group eyes-happy">
          <path class="eye-stroke" d="M8.8 18.4 Q12.2 12.6 15.6 18.4" />
          <path class="eye-stroke" d="M20.4 18.4 Q23.8 12.6 27.2 18.4" />
        </g>

        <!-- 表情 3: 俏皮单眼 Wink -->
        <g v-else-if="expression === 'wink'" key="wink" class="eyes-group eyes-wink">
          <rect class="eye-fill eye-wink-open" x="10.8" y="11.8" width="5.4" height="9.4" rx="2.7" />
          <path class="eye-stroke eye-wink-closed" d="M26.2 13.8 L21.0 16.5 L26.2 19.2" />
        </g>

        <!-- 表情 4: 左顾右盼好奇扫视 -->
        <g v-else-if="expression === 'look'" key="look" class="eyes-group eyes-look">
          <g class="look-gaze">
            <rect class="eye-fill" x="11.0" y="11.8" width="5.2" height="9.2" rx="2.6" />
            <rect class="eye-fill" x="19.8" y="11.8" width="5.2" height="9.2" rx="2.6" />
          </g>
        </g>

        <!-- 表情 5: 连眨卖萌眼 -->
        <g v-else-if="expression === 'blink'" key="blink" class="eyes-group eyes-blink">
          <rect class="eye-fill eye-double-blink" x="11.0" y="11.8" width="5.4" height="9.4" rx="2.7" />
          <rect class="eye-fill eye-double-blink" x="19.6" y="11.8" width="5.4" height="9.4" rx="2.7" />
        </g>

        <!-- 表情 6: 闪亮星星眼 -->
        <g v-else-if="expression === 'sparkle'" key="sparkle" class="eyes-group eyes-sparkle">
          <path
            class="eye-fill eye-star eye-star-left"
            d="M12.4 11.4 L13.8 15.0 L17.0 16.4 L13.8 17.8 L12.4 21.4 L11.0 17.8 L7.8 16.4 L11.0 15.0 Z"
          />
          <path
            class="eye-fill eye-star eye-star-right"
            d="M23.6 11.4 L25.0 15.0 L28.2 16.4 L25.0 17.8 L23.6 21.4 L22.2 17.8 L19.0 16.4 L22.2 15.0 Z"
          />
        </g>

        <!-- 表情 7: 歪头大小眼 -->
        <g v-else-if="expression === 'curious'" key="curious" class="eyes-group eyes-curious">
          <rect class="eye-fill eye-curious-small" x="11.2" y="13.8" width="4.8" height="6.6" rx="2.4" />
          <rect class="eye-fill eye-curious-big" x="20.0" y="11.2" width="5.8" height="10.4" rx="2.9" />
        </g>

        <!-- 表情 8: 专注微眯眼 -->
        <g v-else-if="expression === 'focused'" key="focused" class="eyes-group eyes-focused">
          <g class="focused-scan">
            <rect class="eye-fill" x="10.0" y="14.4" width="6.0" height="5.0" rx="2.3" />
            <rect class="eye-fill" x="20.0" y="14.4" width="6.0" height="5.0" rx="2.3" />
          </g>
        </g>

        <!-- 表情 9: 惊讶瞪大眼 -->
        <g v-else-if="expression === 'surprised'" key="surprised" class="eyes-group eyes-surprised">
          <rect class="eye-fill eye-surprised" x="10.0" y="11.0" width="6.2" height="10.8" rx="3.1" />
          <rect class="eye-fill eye-surprised" x="19.8" y="11.0" width="6.2" height="10.8" rx="3.1" />
        </g>

        <!-- 表情 10: 犯困打盹眼 -->
        <g v-else-if="expression === 'sleepy'" key="sleepy" class="eyes-group eyes-sleepy">
          <rect class="eye-fill eye-sleepy" x="9.8" y="16.4" width="6.2" height="3.0" rx="1.5" />
          <rect class="eye-fill eye-sleepy" x="20.0" y="16.4" width="6.2" height="3.0" rx="1.5" />
        </g>

        <!-- 表情 11: 心动爱心眼 -->
        <g v-else key="love" class="eyes-group eyes-love">
          <path
            class="eye-fill eye-heart eye-heart-left"
            d="M12.4 20.8 C12.4 20.8 8.6 18.0 8.6 15.1 C8.6 13.6 9.7 12.5 11.0 12.5 C11.7 12.5 12.1 12.9 12.4 13.5 C12.7 12.9 13.1 12.5 13.8 12.5 C15.1 12.5 16.2 13.6 16.2 15.1 C16.2 18.0 12.4 20.8 12.4 20.8 Z"
          />
          <path
            class="eye-fill eye-heart eye-heart-right"
            d="M23.6 20.8 C23.6 20.8 19.8 18.0 19.8 15.1 C19.8 13.6 20.9 12.5 22.2 12.5 C22.9 12.5 23.3 12.9 23.6 13.5 C23.9 12.9 24.3 12.5 25.0 12.5 C26.3 12.5 27.4 13.6 27.4 15.1 C27.4 18.0 23.6 20.8 23.6 20.8 Z"
          />
        </g>
      </g>
    </svg>
  </span>
</template>

<style scoped>
.assistant-avatar {
  --bot-color: var(--assistant-color, #3c82f6);
  --eye-color: var(--assistant-eye-color, #ffffff);
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
  filter: drop-shadow(0 1.5px 3.5px color-mix(in srgb, var(--bot-color) 36%, transparent));
  animation: avatar-float 3s ease-in-out infinite;
}

.state-thinking .face-svg,
.state-reading .face-svg {
  animation-duration: 1.2s;
  filter: drop-shadow(0 0 5px color-mix(in srgb, var(--bot-color) 62%, transparent));
}

.avatar-shell {
  transform-box: fill-box;
  transform-origin: center;
}

.avatar-body {
  fill: var(--bot-color);
}

.eye-fill {
  fill: var(--eye-color);
}

.eye-stroke {
  stroke: var(--eye-color);
  stroke-width: 2.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.eyes-group {
  transform-box: fill-box;
  transform-origin: center;
  animation: expr-pop-in 260ms cubic-bezier(0.22, 1.35, 0.36, 1) both;
}

/* 1. 招牌右看：自然眨眼 + 轻微视线晃动 */
.peek-gaze {
  animation: peek-gaze 3.2s ease-in-out infinite;
}

.eye-peek-blink {
  transform-box: fill-box;
  transform-origin: center;
  animation: peek-blink 2.8s ease-in-out infinite;
}

/* 2. 开心弯月眼：轻快弹跳 */
.expr-happy .avatar-shell {
  animation: body-happy 1.35s ease-in-out infinite;
}

.eyes-happy {
  animation: expr-pop-in 260ms cubic-bezier(0.22, 1.35, 0.36, 1) both,
    happy-bounce 1.35s ease-in-out infinite;
}

/* 3. 俏皮 Wink：微歪头 + 单眼眨动 */
.expr-wink .avatar-shell {
  animation: body-wink 1.9s ease-in-out infinite;
}

.eye-wink-open {
  transform-box: fill-box;
  transform-origin: center;
  animation: wink-open-pulse 1.9s ease-in-out infinite;
}

.eye-wink-closed {
  transform-box: fill-box;
  transform-origin: center;
  animation: wink-squeeze 1.9s ease-in-out infinite;
}

/* 4. 左顾右盼：双眼左右扫视 + 本体微倾 */
.expr-look .avatar-shell {
  animation: body-look 2.4s ease-in-out infinite;
}

.look-gaze {
  animation: look-scan 2.4s ease-in-out infinite;
}

/* 5. 连眨卖萌：灵动双连眨 */
.eye-double-blink {
  transform-box: fill-box;
  transform-origin: center;
  animation: double-blink 2.4s ease-in-out infinite;
}

/* 6. 闪亮星星眼：错峰闪烁旋转 */
.expr-sparkle .avatar-shell {
  animation: body-sparkle 1.4s ease-in-out infinite;
}

.eye-star {
  transform-box: fill-box;
  transform-origin: center;
  animation: sparkle-twinkle 1.15s ease-in-out infinite;
}

.eye-star-right {
  animation-delay: 0.22s;
}

/* 7. 歪头大小眼：歪头好奇 + 大眼放大呼吸 */
.expr-curious .avatar-shell {
  animation: body-curious 2.2s ease-in-out infinite;
}

.eye-curious-small {
  transform-box: fill-box;
  transform-origin: center;
  animation: curious-squint 2.2s ease-in-out infinite;
}

.eye-curious-big {
  transform-box: fill-box;
  transform-origin: center;
  animation: curious-widen 2.2s ease-in-out infinite;
}

/* 8. 专注微眯眼：平稳扫读 */
.focused-scan {
  animation: focused-scan 1.75s ease-in-out infinite;
}

/* 9. 惊讶瞪大眼：惊奇微震放大 */
.expr-surprised .avatar-shell {
  animation: body-surprised 1.4s ease-in-out infinite;
}

.eye-surprised {
  transform-box: fill-box;
  transform-origin: center;
  animation: surprised-pop 1.4s ease-in-out infinite;
}

/* 10. 犯困打盹眼：缓慢点头打盹 */
.expr-sleepy .avatar-shell {
  animation: body-sleepy 2.8s ease-in-out infinite;
}

.eye-sleepy {
  transform-box: fill-box;
  transform-origin: center;
  animation: sleepy-breathe 2.8s ease-in-out infinite;
}

/* 11. 心动爱心眼：心跳节奏律动 */
.expr-love .avatar-shell {
  animation: body-love 1.25s ease-in-out infinite;
}

.eye-heart {
  transform-box: fill-box;
  transform-origin: center;
  animation: love-beat 1.25s ease-in-out infinite;
}

.eye-heart-right {
  animation-delay: 0.14s;
}

@keyframes avatar-float {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-1.3px);
  }
}

@keyframes expr-pop-in {
  0% {
    opacity: 0.35;
    transform: scale(0.84);
  }
  100% {
    opacity: 1;
    transform: scale(1);
  }
}

@keyframes peek-gaze {
  0%, 100% {
    transform: translateX(0);
  }
  35% {
    transform: translateX(0.9px);
  }
  70% {
    transform: translateX(-1.3px);
  }
}

@keyframes peek-blink {
  0%, 40%, 50%, 100% {
    transform: scaleY(1);
  }
  45% {
    transform: scaleY(0.12);
  }
}

@keyframes body-happy {
  0%, 100% {
    transform: translateY(0) scale(1, 1);
  }
  45% {
    transform: translateY(-1.4px) scale(0.97, 1.04);
  }
  70% {
    transform: translateY(0.4px) scale(1.03, 0.97);
  }
}

@keyframes happy-bounce {
  0%, 100% {
    transform: translateY(0);
  }
  45% {
    transform: translateY(-1.2px) scale(1.04);
  }
}

@keyframes body-wink {
  0%, 100% {
    transform: rotate(0deg);
  }
  35%, 65% {
    transform: rotate(-5.5deg);
  }
}

@keyframes wink-open-pulse {
  0%, 100% {
    transform: scaleY(1);
  }
  50% {
    transform: scaleY(1.07);
  }
}

@keyframes wink-squeeze {
  0%, 100% {
    transform: scale(1, 1);
  }
  45% {
    transform: scale(1.08, 0.84);
  }
}

@keyframes body-look {
  0%, 100% {
    transform: rotate(0deg);
  }
  28% {
    transform: rotate(-3.5deg);
  }
  72% {
    transform: rotate(3.5deg);
  }
}

@keyframes look-scan {
  0%, 100% {
    transform: translate(0, 0);
  }
  25%, 35% {
    transform: translate(-2.6px, -0.3px);
  }
  65%, 75% {
    transform: translate(2.8px, -0.5px);
  }
}

@keyframes double-blink {
  0%, 28%, 38%, 48%, 58%, 100% {
    transform: scale(1, 1);
  }
  33%, 53% {
    transform: scale(1.06, 0.12);
  }
}

@keyframes body-sparkle {
  0%, 100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.04);
  }
}

@keyframes sparkle-twinkle {
  0%, 100% {
    transform: scale(0.84) rotate(-6deg);
    opacity: 0.86;
  }
  50% {
    transform: scale(1.16) rotate(10deg);
    opacity: 1;
  }
}

@keyframes body-curious {
  0%, 100% {
    transform: rotate(0deg);
  }
  35%, 65% {
    transform: rotate(7deg);
  }
}

@keyframes curious-squint {
  0%, 100% {
    transform: scaleY(1);
  }
  50% {
    transform: scaleY(0.84);
  }
}

@keyframes curious-widen {
  0%, 100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.09);
  }
}

@keyframes focused-scan {
  0%, 100% {
    transform: translateX(-1.4px);
  }
  50% {
    transform: translateX(1.8px);
  }
}

@keyframes body-surprised {
  0%, 100% {
    transform: scale(1);
  }
  25% {
    transform: scale(1.05, 0.96);
  }
  55% {
    transform: scale(0.98, 1.04);
  }
}

@keyframes surprised-pop {
  0%, 100% {
    transform: scale(1);
  }
  35% {
    transform: scale(1.1);
  }
  65% {
    transform: scale(0.96);
  }
}

@keyframes body-sleepy {
  0%, 100% {
    transform: translateY(0) rotate(0deg);
  }
  55% {
    transform: translateY(1.3px) rotate(-4deg);
  }
}

@keyframes sleepy-breathe {
  0%, 100% {
    opacity: 0.72;
    transform: scaleY(0.85);
  }
  55% {
    opacity: 1;
    transform: scaleY(1.12);
  }
}

@keyframes body-love {
  0%, 100% {
    transform: scale(1);
  }
  30% {
    transform: scale(1.05);
  }
  60% {
    transform: scale(0.98);
  }
}

@keyframes love-beat {
  0%, 100% {
    transform: scale(0.9);
  }
  30% {
    transform: scale(1.14);
  }
  52% {
    transform: scale(0.96);
  }
  72% {
    transform: scale(1.08);
  }
}

/* 在系统减少动效偏好下依然保留小Y头像表情微动效。 */
@media (prefers-reduced-motion: reduce) {
  .face-svg {
    animation-duration: 3s !important;
    animation-iteration-count: infinite !important;
  }
  .peek-gaze,
  .eye-peek-blink,
  .expr-happy .avatar-shell,
  .eyes-happy,
  .expr-wink .avatar-shell,
  .eye-wink-open,
  .eye-wink-closed,
  .expr-look .avatar-shell,
  .look-gaze,
  .eye-double-blink,
  .expr-sparkle .avatar-shell,
  .eye-star,
  .expr-curious .avatar-shell,
  .eye-curious-small,
  .eye-curious-big,
  .focused-scan,
  .expr-surprised .avatar-shell,
  .eye-surprised,
  .expr-sleepy .avatar-shell,
  .eye-sleepy,
  .expr-love .avatar-shell,
  .eye-heart {
    animation-iteration-count: infinite !important;
  }
  .peek-gaze { animation-duration: 3.2s !important; }
  .eye-peek-blink { animation-duration: 2.8s !important; }
  .expr-happy .avatar-shell, .eyes-happy { animation-duration: 1.35s !important; }
  .expr-wink .avatar-shell, .eye-wink-open, .eye-wink-closed { animation-duration: 1.9s !important; }
  .expr-look .avatar-shell, .look-gaze { animation-duration: 2.4s !important; }
  .eye-double-blink { animation-duration: 2.4s !important; }
  .expr-sparkle .avatar-shell { animation-duration: 1.4s !important; }
  .eye-star { animation-duration: 1.15s !important; }
  .expr-curious .avatar-shell, .eye-curious-small, .eye-curious-big { animation-duration: 2.2s !important; }
  .focused-scan { animation-duration: 1.75s !important; }
  .expr-surprised .avatar-shell, .eye-surprised { animation-duration: 1.4s !important; }
  .expr-sleepy .avatar-shell, .eye-sleepy { animation-duration: 2.8s !important; }
  .expr-love .avatar-shell, .eye-heart { animation-duration: 1.25s !important; }
}
</style>
