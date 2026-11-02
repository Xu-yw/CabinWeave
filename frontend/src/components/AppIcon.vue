<!-- 统一的线性图标组件：本地 SVG 路径，不依赖图标库和网络请求。 -->
<template>
  <svg class="app-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path v-for="(path, index) in paths[name] || paths.dot" :key="index" :d="path" />
  </svg>
</template>

<script setup>
defineProps({ name: { type: String, default: 'dot' } })

const paths = {
  dot: ['M12 12h.01'],
  car: ['M5 16V11l2-5h10l2 5v5', 'M3 16h18v3H3z', 'M7 19v2M17 19v2', 'M7.5 13h.01M16.5 13h.01'],
  chart: ['M4 3v17h17', 'M9 15v-4M14 15V7M19 15V4'],
  system: ['M4 5h16v10H4z', 'M9 19h6M12 15v4'],
  send: ['M12 19V5', 'm6 11 6-6 6 6'],
  reset: ['M4 4v5h5', 'M5.5 15a7.5 7.5 0 1 0 1.3-8.3L4 9'],
  plus: ['M12 5v14M5 12h14'],
  panel: ['M4 4h16v16H4z', 'M15 4v16'],
  bolt: ['M13 2 4 14h7l-1 8 9-12h-7l1-8Z'],
  route: ['M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z', 'M18 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z', 'M8 17h7a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h7'],
  block: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z', 'm5.7 5.7 12.6 12.6'],
  search: ['m20 20-4.5-4.5', 'M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0Z'],
  refresh: ['M20 7v5h-5', 'M4 17v-5h5', 'M6.1 7a8 8 0 0 1 13 1M4.9 16a8 8 0 0 0 13 1'],
  user: ['M16 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z', 'M5 21v-1a7 7 0 0 1 14 0v1']
}
</script>
