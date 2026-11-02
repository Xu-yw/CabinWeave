<!-- 车速仪表：270° 圆弧，刻度 0–200 km/h，中间是滚动变化的车速；本轮车速变化时圆弧描琥珀色。 -->
<template>
  <div :class="['gauge', { changed }]" role="img" :aria-label="`车速 ${speed} km/h`">
    <svg viewBox="0 0 120 106" aria-hidden="true">
      <path class="track" :d="ARC" pathLength="100" />
      <path class="value" :d="ARC" pathLength="100" :style="{ strokeDashoffset: 100 - pct, opacity: pct > 0 ? 1 : 0 }" />
      <line v-for="t in TICKS" :key="t.key" :class="['tick', { passed: t.value <= speed }]" :x1="t.x1" :y1="t.y1" :x2="t.x2" :y2="t.y2" />
    </svg>
    <div class="readout">
      <b class="num"><NumberFlow :value="speed" /></b>
      <small>km/h</small>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import NumberFlow from '@number-flow/vue'

const props = defineProps({
  speed: { type: Number, default: 0 },
  changed: { type: Boolean, default: false }
})

const MAX = 200
const CX = 60
const CY = 60
const R = 50

// 从左下 135° 顺时针画到右下 45°
const point = (deg, r) => [CX + r * Math.cos((deg * Math.PI) / 180), CY + r * Math.sin((deg * Math.PI) / 180)]
const [sx, sy] = point(135, R)
const [ex, ey] = point(45, R)
const ARC = `M${sx.toFixed(2)} ${sy.toFixed(2)} A${R} ${R} 0 1 1 ${ex.toFixed(2)} ${ey.toFixed(2)}`

const TICKS = Array.from({ length: 11 }, (_, i) => {
  const deg = 135 + i * 27
  const [x1, y1] = point(deg, 39)
  const [x2, y2] = point(deg, i % 5 === 0 ? 34 : 36)
  return { key: i, value: i * 20, x1, y1, x2, y2 }
})

const pct = computed(() => Math.max(0, Math.min(100, (props.speed / MAX) * 100)))
</script>

<style scoped>
.gauge { position: relative; width: 124px; flex: none; }
svg { display: block; width: 100%; }
.track, .value { fill: none; stroke-width: 6; stroke-linecap: round; }
.track { stroke: var(--line); }
.value { stroke: var(--ice); stroke-dasharray: 100 100; transition: stroke-dashoffset .8s cubic-bezier(.2, .8, .2, 1), stroke .3s; }
.tick { stroke: var(--ink-3); stroke-width: 1.2; opacity: .45; }
.tick.passed { stroke: var(--ink-2); opacity: .8; }
.changed .value { stroke: var(--amber); }

.readout { position: absolute; inset: 0 0 8px; display: flex; flex-direction: column; align-items: center; justify-content: center; line-height: 1; }
.readout b { font-size: 34px; font-weight: 600; }
.readout small { margin-top: 4px; color: var(--ink-3); font-size: 11px; }
.changed .readout b { color: var(--amber); }
</style>
