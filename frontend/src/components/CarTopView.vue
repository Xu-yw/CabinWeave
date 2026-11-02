<!-- 车身俯视图（车头朝上）：车窗与天窗开度、后备箱、车锁、双闪、氛围灯和四轮胎压随车辆状态变化。
     车窗和天窗按开度露出夜色并平滑过渡；本轮变化的部件描琥珀色并发光，胎压低于 200 kPa 的车轮标红。 -->
<template>
  <svg class="car-top" viewBox="0 0 260 422" role="img" :aria-label="summary">
    <defs>
      <linearGradient id="cw-body" x1="0" x2="1" y1="0" y2="0">
        <stop offset="0" stop-color="#151f2b" />
        <stop offset=".2" stop-color="#213041" />
        <stop offset=".5" stop-color="#2a3b4f" />
        <stop offset=".8" stop-color="#213041" />
        <stop offset="1" stop-color="#151f2b" />
      </linearGradient>
      <linearGradient id="cw-glass" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="#7cc6e6" stop-opacity=".34" />
        <stop offset="1" stop-color="#7cc6e6" stop-opacity=".1" />
      </linearGradient>
      <linearGradient id="cw-roof" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="#7cc6e6" stop-opacity=".18" />
        <stop offset="1" stop-color="#7cc6e6" stop-opacity=".05" />
      </linearGradient>
      <radialGradient id="cw-floor">
        <stop offset="0" stop-color="#7cc6e6" stop-opacity=".13" />
        <stop offset=".7" stop-color="#7cc6e6" stop-opacity=".03" />
        <stop offset="1" stop-color="#7cc6e6" stop-opacity="0" />
      </radialGradient>
      <radialGradient id="cw-shadow">
        <stop offset=".6" stop-color="#05080d" stop-opacity=".7" />
        <stop offset="1" stop-color="#05080d" stop-opacity="0" />
      </radialGradient>
      <radialGradient id="cw-sheen">
        <stop offset="0" stop-color="#ffffff" stop-opacity=".09" />
        <stop offset="1" stop-color="#ffffff" stop-opacity="0" />
      </radialGradient>
      <filter id="cw-glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="2.4" result="blur" />
        <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
      </filter>
    </defs>

    <!-- 地面：一圈冷光和车身投影 -->
    <ellipse cx="130" cy="212" rx="128" ry="208" fill="url(#cw-floor)" />
    <ellipse cx="130" cy="213" rx="82" ry="200" fill="url(#cw-shadow)" />

    <!-- 车轮（先画，车身盖住内侧一半）：fl 左前、fr 右前、rl 左后、rr 右后 -->
    <rect
      v-for="w in WHEELS" :key="w.key"
      :class="['wheel', { low: tires[w.key] < LOW_TIRE, changed: isChanged(`tire_pressure_kpa.${w.key}`) }]"
      :x="w.x" :y="w.y" width="16" height="48" rx="6"
    />

    <path class="mirror" d="M66 136 L52 132 Q48 139 52 146 L66 146 Z" />
    <path class="mirror" d="M194 136 L208 132 Q212 139 208 146 L194 146 Z" />

    <path
      class="body"
      d="M130 26 C161 26 180 30 187 44 C192 58 196 76 196 96 C197 180 195 230 196 300 C197 340 196 368 187 384
         C175 394 154 396 130 396 C106 396 85 394 73 384 C64 368 63 340 64 300 C65 230 63 180 64 96
         C64 76 68 58 73 44 C80 30 99 26 130 26 Z"
    />
    <ellipse cx="130" cy="80" rx="46" ry="38" fill="url(#cw-sheen)" />
    <path class="crease" d="M102 52 C106 78 108 100 110 118 M158 52 C154 78 152 100 150 118" />

    <!-- 前后贯穿灯带 -->
    <path class="lamp-front" d="M82 50 Q130 32 178 50" />
    <path class="lamp-rear" d="M82 380 Q130 394 178 380" />

    <!-- 前挡风与后窗 -->
    <path class="glass" d="M92 128 Q130 116 168 128 L178 170 Q130 162 82 170 Z" />
    <path class="streak" d="M106 127 L119 123 L101 166 L89 168 Z" />
    <path class="glass" d="M84 298 L176 298 L168 322 Q130 330 92 322 Z" />

    <!-- 全景车顶，前段是可开启的天窗：开启部分露出夜色 -->
    <rect class="roof" x="84" y="174" width="92" height="120" rx="10" />
    <path class="streak faint" d="M150 176 L166 176 L120 292 L104 292 Z" />
    <g :class="{ changed: isChanged('body.sunroof') }">
      <rect class="opening" x="92" y="180" width="76" height="58" rx="7" :style="{ transform: `scaleY(${body.sunroof / 100 || 0})` }" />
      <rect class="outline" x="92" y="180" width="76" height="58" rx="7" />
    </g>

    <!-- 氛围灯：车内一圈柔光 -->
    <rect v-if="lights.ambient" class="ambient" x="79" y="168" width="102" height="132" rx="14" />

    <!-- 侧窗：开度从上往下露出夜色，旁边标百分比 -->
    <g v-for="w in WINDOWS" :key="w.key" :class="{ changed: isChanged(`body.windows.${w.key}`) }">
      <rect class="glass" :x="w.x" :y="w.y" width="8" :height="w.h" rx="3" />
      <rect class="opening" :x="w.x" :y="w.y" width="8" :height="w.h" rx="3" :style="{ transform: `scaleY(${(windows[w.key] || 0) / 100})` }" />
      <rect class="outline" :x="w.x" :y="w.y" width="8" :height="w.h" rx="3" />
      <text v-if="windows[w.key] > 0" class="pct num" :x="w.labelX" :y="w.y + w.h / 2 + 5" :text-anchor="w.anchor">{{ windows[w.key] }}%</text>
    </g>

    <!-- 后备箱：打开时尾门向后掀起 -->
    <g :class="['trunk', { open: body.trunk_open, changed: isChanged('body.trunk_open') }]">
      <path class="lid" d="M90 330 Q130 336 170 330 L176 374 Q130 386 84 374 Z" />
    </g>

    <!-- 车锁：绿色已锁，琥珀色未锁 -->
    <g :class="['lock', { unlocked: !body.doors_locked, changed: isChanged('body.doors_locked') }]" transform="translate(130 262)">
      <rect x="-9" y="-2" width="18" height="14" rx="3" />
      <path :d="body.doors_locked ? 'M-5 -2 V-7 a5 5 0 0 1 10 0 V-2' : 'M-5 -2 V-7 a5 5 0 0 1 10 0 V-9'" />
    </g>

    <!-- 双闪：四角转向灯 -->
    <g :class="['hazard', { on: lights.hazard, changed: isChanged('lights.hazard') }]">
      <rect x="70" y="56" width="12" height="6" rx="3" />
      <rect x="178" y="56" width="12" height="6" rx="3" />
      <rect x="72" y="366" width="12" height="6" rx="3" />
      <rect x="176" y="366" width="12" height="6" rx="3" />
    </g>

    <!-- 胎压读数 -->
    <text
      v-for="w in WHEELS" :key="`t-${w.key}`"
      :class="['tire', 'num', { low: tires[w.key] < LOW_TIRE, changed: isChanged(`tire_pressure_kpa.${w.key}`) }]"
      :x="w.labelX" :y="w.y + 29" :text-anchor="w.anchor"
    >{{ tires[w.key] }}</text>
    <text class="legend" x="130" y="418" text-anchor="middle">胎压 kPa</text>
  </svg>
</template>

<script setup>
import { computed } from 'vue'
import { isChanged } from '../lib/session'

const props = defineProps({ state: { type: Object, required: true } })

const LOW_TIRE = 200

const body = computed(() => props.state.body || {})
const windows = computed(() => body.value.windows || {})
const lights = computed(() => props.state.lights || {})
const tires = computed(() => props.state.tire_pressure_kpa || {})

const WHEELS = [
  { key: 'fl', x: 56, y: 86, labelX: 46, anchor: 'end' },
  { key: 'fr', x: 188, y: 86, labelX: 214, anchor: 'start' },
  { key: 'rl', x: 56, y: 294, labelX: 46, anchor: 'end' },
  { key: 'rr', x: 188, y: 294, labelX: 214, anchor: 'start' }
]

const WINDOWS = [
  { key: 'fl', x: 72, y: 176, h: 56, labelX: 46, anchor: 'end' },
  { key: 'rl', x: 72, y: 238, h: 52, labelX: 46, anchor: 'end' },
  { key: 'fr', x: 180, y: 176, h: 56, labelX: 214, anchor: 'start' },
  { key: 'rr', x: 180, y: 238, h: 52, labelX: 214, anchor: 'start' }
]

const WHEEL_NAMES = { fl: '左前', fr: '右前', rl: '左后', rr: '右后' }

const summary = computed(() => {
  const open = Object.entries(windows.value).filter(([, v]) => v > 0).length
  const lowTires = Object.entries(tires.value).filter(([, v]) => v < LOW_TIRE)
  return [
    open ? `${open} 个车窗打开` : '车窗全关',
    body.value.sunroof > 0 ? `天窗开 ${body.value.sunroof}%` : '天窗关闭',
    body.value.trunk_open ? '后备箱打开' : '后备箱关闭',
    body.value.doors_locked ? '车门已锁' : '车门未锁',
    lights.value.hazard ? '双闪开启' : '',
    lights.value.ambient ? '氛围灯开启' : '',
    ...lowTires.map(([k, v]) => `${WHEEL_NAMES[k] || k}胎压偏低（${v} kPa）`)
  ].filter(Boolean).join('，')
})
</script>

<style scoped>
.car-top { width: 100%; max-height: 330px; display: block; margin: 4px 0; }

.body { fill: url(#cw-body); stroke: #3a4d64; stroke-width: 1.2; }
.crease { fill: none; stroke: rgba(255, 255, 255, .06); stroke-width: 1.2; }
.mirror { fill: #1d2a39; stroke: #3a4d64; stroke-width: 1; }

.wheel { fill: #0a1018; stroke: #2d3b4d; stroke-width: 1.2; transition: stroke .3s, fill .3s; }
.wheel.low { fill: #2a1418; stroke: var(--red); }

.glass { fill: url(#cw-glass); }
.roof { fill: url(#cw-roof); stroke: rgba(124, 198, 230, .22); stroke-width: 1; }
.streak { fill: #ffffff; opacity: .07; }
.streak.faint { opacity: .035; }
.opening { fill: #0a0f17; transform-box: fill-box; transform-origin: 50% 0; transition: transform .6s cubic-bezier(.2, .8, .2, 1); }
.outline { fill: none; stroke: rgba(124, 198, 230, .42); stroke-width: 1; }
.pct { fill: var(--ink-2); font-size: 15px; }

.lamp-front { fill: none; stroke: #dff1ff; stroke-width: 2.6; stroke-linecap: round; opacity: .85; filter: url(#cw-glow); }
.lamp-rear { fill: none; stroke: #8f3a44; stroke-width: 2.6; stroke-linecap: round; opacity: .8; }

.lid { fill: #22334a; stroke: #3a4d64; stroke-width: 1.2; transition: transform .45s ease, fill .3s; transform-origin: 130px 330px; }
.trunk.open .lid { transform: translateY(28px) scaleY(.72); fill: rgba(242, 168, 59, .22); }

.lock rect, .lock path { fill: none; stroke: var(--green); stroke-width: 1.6; }
.lock.unlocked rect, .lock.unlocked path { stroke: var(--amber); }

.hazard rect { fill: #3a2f20; }
.hazard.on rect { fill: var(--amber); filter: url(#cw-glow); animation: blink 1s steps(1, end) infinite; }
@keyframes blink { 50% { opacity: .2; } }

.ambient { fill: none; stroke: #9b8cff; stroke-width: 2.4; opacity: .75; filter: url(#cw-glow); }

.tire { fill: var(--ink-2); font-size: 15px; }
.tire.low { fill: var(--red); font-weight: 700; }
.legend { fill: var(--ink-3); font-size: 12px; }

/* 本轮变化的部件：描琥珀色并发光三下（对应 3 秒高亮） */
.changed .outline, .changed .lid, .wheel.changed, .changed rect, .changed path { stroke: var(--amber); stroke-width: 2; }
.changed .outline, .changed .lid, .wheel.changed, .lock.changed { animation: flare 1s ease-in-out 3; }
.changed .pct, .tire.changed { fill: var(--amber); }
@keyframes flare {
  50% { filter: drop-shadow(0 0 5px rgba(242, 168, 59, .85)); }
}
</style>
