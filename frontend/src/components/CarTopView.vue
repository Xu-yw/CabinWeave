<!-- 车身俯视图（车头朝上）：四个车窗开度、天窗、后备箱、车锁、双闪和氛围灯随车辆状态变化，本轮变化的部件描琥珀色。 -->
<template>
  <svg class="car-top" viewBox="0 0 220 400" role="img" :aria-label="summary">
    <!-- 氛围灯：车内一圈柔光 -->
    <rect v-if="lights.ambient" class="ambient" x="66" y="136" width="88" height="140" rx="18" />

    <path class="body" d="M72 30 Q110 14 148 30 L160 72 L164 330 Q162 372 110 378 Q58 372 56 330 L60 72 Z" />
    <ellipse class="mirror" cx="50" cy="122" rx="9" ry="6" />
    <ellipse class="mirror" cx="170" cy="122" rx="9" ry="6" />

    <path class="glass" d="M78 100 Q110 92 142 100 L152 138 L68 138 Z" />
    <path class="glass" d="M70 274 L150 274 L143 300 Q110 306 77 300 Z" />

    <!-- 天窗：开启部分露出夜色 -->
    <g :class="{ changed: isChanged('body.sunroof') }">
      <rect class="glass" x="84" y="156" width="52" height="66" rx="7" />
      <rect v-if="body.sunroof > 0" class="opening" x="84" y="156" width="52" :height="66 * body.sunroof / 100" rx="7" />
      <rect class="outline" x="84" y="156" width="52" height="66" rx="7" />
    </g>

    <!-- 侧窗：fl 主驾、fr 副驾、rl 左后、rr 右后 -->
    <g v-for="w in windowShapes" :key="w.key" :class="{ changed: isChanged(`body.windows.${w.key}`) }">
      <rect class="glass" :x="w.x" :y="w.y" width="9" :height="w.h" rx="3" />
      <rect v-if="windows[w.key] > 0" class="opening" :x="w.x" :y="w.y" width="9" :height="w.h * windows[w.key] / 100" rx="3" />
      <rect class="outline" :x="w.x" :y="w.y" width="9" :height="w.h" rx="3" />
      <text v-if="windows[w.key] > 0" class="pct num" :x="w.labelX" :y="w.y + w.h / 2 + 4" :text-anchor="w.anchor">{{ windows[w.key] }}%</text>
    </g>

    <!-- 后备箱：打开时尾门向后掀起 -->
    <g :class="['trunk', { open: body.trunk_open, changed: isChanged('body.trunk_open') }]">
      <path class="lid" d="M74 308 Q110 314 146 308 L150 350 Q110 362 70 350 Z" />
    </g>

    <!-- 车锁 -->
    <g :class="['lock', { unlocked: !body.doors_locked, changed: isChanged('body.doors_locked') }]" transform="translate(110 246)">
      <rect x="-9" y="-2" width="18" height="14" rx="3" />
      <path :d="body.doors_locked ? 'M-5 -2 V-7 a5 5 0 0 1 10 0 V-2' : 'M-5 -2 V-7 a5 5 0 0 1 10 0 V-9'" />
    </g>

    <!-- 大灯与双闪 -->
    <path class="headlight" d="M76 34 L92 28 M144 34 L128 28" />
    <g :class="['hazard', { on: lights.hazard, changed: isChanged('lights.hazard') }]">
      <rect x="62" y="44" width="12" height="7" rx="3" />
      <rect x="146" y="44" width="12" height="7" rx="3" />
      <rect x="60" y="352" width="12" height="7" rx="3" />
      <rect x="148" y="352" width="12" height="7" rx="3" />
    </g>
  </svg>
</template>

<script setup>
import { computed } from 'vue'
import { isChanged } from '../lib/session'

const props = defineProps({ state: { type: Object, required: true } })

const body = computed(() => props.state.body || {})
const windows = computed(() => body.value.windows || {})
const lights = computed(() => props.state.lights || {})

const windowShapes = [
  { key: 'fl', x: 59, y: 144, h: 58, labelX: 40, anchor: 'end' },
  { key: 'rl', x: 59, y: 210, h: 56, labelX: 40, anchor: 'end' },
  { key: 'fr', x: 152, y: 144, h: 58, labelX: 180, anchor: 'start' },
  { key: 'rr', x: 152, y: 210, h: 56, labelX: 180, anchor: 'start' }
]

const summary = computed(() => {
  const open = Object.entries(windows.value).filter(([, v]) => v > 0).length
  return [
    open ? `${open} 个车窗打开` : '车窗全关',
    body.value.sunroof > 0 ? `天窗开 ${body.value.sunroof}%` : '天窗关闭',
    body.value.trunk_open ? '后备箱打开' : '后备箱关闭',
    body.value.doors_locked ? '车门已锁' : '车门未锁',
    lights.value.hazard ? '双闪开启' : ''
  ].filter(Boolean).join('，')
})
</script>

<style scoped>
.car-top { width: 100%; max-height: 300px; display: block; }
.body { fill: #1f2d3d; stroke: #3a4d64; stroke-width: 1.5; }
.mirror { fill: #1f2d3d; stroke: #3a4d64; }
.glass { fill: rgba(124, 198, 230, 0.22); }
.opening { fill: var(--night); }
.outline { fill: none; stroke: rgba(124, 198, 230, 0.45); stroke-width: 1; }
.pct { fill: var(--ink-2); font-size: 12px; }
.changed .outline, .changed .lid, .changed rect { stroke: var(--amber); stroke-width: 2; }
.changed .pct { fill: var(--amber); }

.lid { fill: #24364a; stroke: #3a4d64; stroke-width: 1.5; transition: transform .4s ease; transform-origin: 110px 308px; }
.trunk.open .lid { transform: translateY(26px) scaleY(.7); fill: rgba(242, 168, 59, 0.2); }

.lock rect { fill: none; stroke: var(--green); stroke-width: 1.6; }
.lock path { fill: none; stroke: var(--green); stroke-width: 1.6; }
.lock.unlocked rect, .lock.unlocked path { stroke: var(--amber); }

.headlight { stroke: rgba(230, 237, 245, 0.55); stroke-width: 3; stroke-linecap: round; }
.hazard rect { fill: #3a2f20; }
.hazard.on rect { fill: var(--amber); animation: blink 1s steps(2, start) infinite; }
@keyframes blink { to { opacity: .25; } }

.ambient { fill: none; stroke: #9b8cff; stroke-width: 3; opacity: .7; filter: drop-shadow(0 0 6px #9b8cff); }
</style>
