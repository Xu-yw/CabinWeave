<!-- 横向条形图（单一序列）：每行一个标签、一根条和数值；scale=log 时按对数刻度画条长，用于跨数量级的延迟对比。 -->
<template>
  <figure class="bars">
    <figcaption v-if="caption">{{ caption }}</figcaption>
    <div v-for="item in items" :key="item.label" class="bar-row" :title="`${item.label}：${item.display}`">
      <span class="bar-label">{{ item.label }}</span>
      <span class="track"><i :class="item.tone || 'ice'" :style="{ width: `${width(item.value)}%` }" /></span>
      <span class="bar-value num">{{ item.display }}</span>
    </div>
    <p v-if="note" class="note">{{ note }}</p>
  </figure>
</template>

<script setup>
const props = defineProps({
  items: { type: Array, required: true },     // [{label, value, display, tone?: 'ice' | 'amber'}]
  max: { type: Number, default: 1 },
  min: { type: Number, default: 0.1 },        // 对数刻度的下限
  scale: { type: String, default: 'linear' },
  caption: { type: String, default: '' },
  note: { type: String, default: '' }
})

function width(value) {
  if (!(value > 0)) return 0
  if (props.scale === 'log') {
    const lo = Math.log10(props.min)
    const hi = Math.log10(props.max)
    return Math.max(1.5, Math.min(100, ((Math.log10(value) - lo) / (hi - lo)) * 100))
  }
  return Math.max(1.5, Math.min(100, (value / props.max) * 100))
}
</script>

<style scoped>
.bars { margin: 0; display: flex; flex-direction: column; gap: 8px; }
figcaption { color: var(--ink-2); font-size: 13px; margin-bottom: 2px; }
.bar-row { display: grid; grid-template-columns: 7.5em minmax(0, 1fr) 5.5em; gap: 10px; align-items: center; }
.bar-label { color: var(--ink-2); font-size: 13px; }
.track { height: 12px; background: var(--raised); border-radius: 4px; overflow: hidden; }
.track i { display: block; height: 100%; border-radius: 0 4px 4px 0; }
.track i.ice { background: var(--ice); }
.track i.amber { background: var(--amber); }
.bar-value { text-align: right; font-size: 14px; }
.bar-row:hover .bar-label { color: var(--ink); }
.note { margin: 0; color: var(--ink-3); font-size: 12px; }
</style>
