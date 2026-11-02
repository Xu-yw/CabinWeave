<!-- 座舱页：左车辆状态、中对话、右链路追踪；右栏可收起成驾驶员视图，窄屏时三栏上下排列。 -->
<template>
  <main :class="['page', 'cockpit', { 'trace-hidden': !showTrace }]">
    <VehiclePanel v-if="session.vehicle" :state="session.vehicle" />
    <section v-else class="panel placeholder">
      <p v-if="session.loadError" class="error-text">{{ session.loadError }}</p>
      <p v-else class="muted">正在读取车辆状态…</p>
      <button v-if="session.loadError" @click="loadCockpit"><AppIcon name="refresh" />重试</button>
    </section>

    <ChatPanel />

    <TracePanel v-if="showTrace" @collapse="toggle(false)" />
    <button v-else class="trace-tab" title="展开链路追踪" @click="toggle(true)"><AppIcon name="panel" />链路追踪</button>
  </main>
</template>

<script setup>
import { ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import ChatPanel from '../components/ChatPanel.vue'
import TracePanel from '../components/TracePanel.vue'
import VehiclePanel from '../components/VehiclePanel.vue'
import { loadCockpit, session } from '../lib/session'

const KEY = 'cabinweave.frontend.trace'

function readPref() {
  try {
    return localStorage.getItem(KEY) !== 'hidden'
  } catch {
    return true
  }
}

const showTrace = ref(readPref())

function toggle(value) {
  showTrace.value = value
  try {
    localStorage.setItem(KEY, value ? 'shown' : 'hidden')
  } catch {
    // 存不了就只在本次页面里生效
  }
}
</script>

<style scoped>
.cockpit {
  display: grid;
  grid-template-columns: minmax(300px, 350px) minmax(0, 1fr) minmax(300px, 380px);
  gap: 12px;
  overflow: hidden;
}
.cockpit.trace-hidden { grid-template-columns: minmax(300px, 350px) minmax(0, 1fr) auto; }
.cockpit > * { min-height: 0; }

.placeholder { align-items: center; justify-content: center; gap: 10px; padding: 24px; }
.trace-tab { writing-mode: vertical-rl; align-self: start; border-radius: 12px; padding: 14px 8px; color: var(--ink-2); }
.trace-tab .app-icon { transform: rotate(90deg); }

@media (max-width: 1100px) {
  .cockpit, .cockpit.trace-hidden { grid-template-columns: 1fr; overflow: visible; height: auto; }
  .cockpit > * { min-height: auto; }
  .cockpit :deep(.chat) { height: 78vh; }
  .trace-tab { writing-mode: horizontal-tb; justify-self: start; }
  .trace-tab .app-icon { transform: none; }
}
</style>
