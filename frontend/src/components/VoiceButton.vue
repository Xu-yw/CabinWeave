<!-- 语音按钮：点一下开始听，说完自动发送；按住说话，松开发送；播报时点一下停止。聆听时外圈随麦克风音量起伏。 -->
<template>
  <Tip :text="hint">
    <button
      type="button"
      :class="['voice-btn', state]"
      :style="{ '--level': voice.level.toFixed(3) }"
      :disabled="disabled && state === 'idle'"
      :aria-label="hint"
      :aria-pressed="voice.listening"
      @pointerdown="onDown"
      @pointerup="onUp"
      @pointerleave="onUp"
      @pointercancel="onUp"
      @click="onClick"
      @contextmenu.prevent
    >
      <AppIcon :name="state === 'speaking' ? 'speaking' : 'mic'" />
    </button>
  </Tip>
</template>

<script setup>
import { computed } from 'vue'
import AppIcon from './AppIcon.vue'
import Tip from './Tip.vue'
import { startListening, stopListening, stopSpeaking, voice } from '../lib/voice'

const props = defineProps({ disabled: { type: Boolean, default: false } })
const emit = defineEmits(['final'])

const HOLD_MS = 400   // 按下超过这个时长算"按住说话"，松开即结束

const state = computed(() => (voice.listening ? 'listening' : voice.speaking ? 'speaking' : 'idle'))
const hint = computed(() => ({
  listening: '正在听，说完会自动发送，点一下提前结束',
  speaking: '正在播报，点一下停止',
  idle: '点一下或按住说话'
})[state.value])

let pressedAt = 0

function toggle() {
  if (voice.speaking) stopSpeaking()
  else if (voice.listening) stopListening()
  else if (!props.disabled) startListening(text => emit('final', text))
}

function onDown(event) {
  if (event.button !== 0) return
  const wasIdle = state.value === 'idle'
  toggle()
  pressedAt = wasIdle && voice.listening ? performance.now() : 0
}

function onUp() {
  if (pressedAt && performance.now() - pressedAt > HOLD_MS) stopListening()
  pressedAt = 0
}

// 鼠标和触屏已在 pointerdown 里处理，click 只响应键盘（detail 为 0）
function onClick(event) {
  if (event.detail === 0) toggle()
}
</script>

<style scoped>
.voice-btn { position: relative; width: 42px; height: 42px; padding: 0; justify-content: center; border-radius: 50%; flex: none; touch-action: none; }
.voice-btn::before, .voice-btn::after { content: ""; position: absolute; inset: -1px; border-radius: 50%; pointer-events: none; opacity: 0; }

.voice-btn.listening { background: var(--ice); border-color: var(--ice); color: var(--night); }
.voice-btn.listening::before { opacity: .5; border: 2px solid var(--ice); transform: scale(calc(1.1 + var(--level) * .55)); transition: transform .08s linear; }
.voice-btn.listening::after { border: 1px solid var(--ice); animation: ripple 1.6s ease-out infinite; }

.voice-btn.speaking { background: var(--ice-soft); border-color: rgba(124, 198, 230, .55); color: var(--ice); }
.voice-btn.speaking::after { border: 1px solid var(--ice); animation: ripple 1.2s ease-out infinite; }

@keyframes ripple {
  from { opacity: .55; transform: scale(1); }
  to { opacity: 0; transform: scale(1.75); }
}
</style>
