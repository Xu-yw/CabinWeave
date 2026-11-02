<!-- CabinWeave 前端外框：顶栏（品牌、页面切换、场景与车辆、用户与新对话）+ 座舱 / 评测 / 系统三个页面。 -->
<template>
  <div class="app">
    <header class="topbar">
      <div class="brand"><BrandMark />CabinWeave</div>
      <nav class="tabs" aria-label="页面">
        <button v-for="tab in tabs" :key="tab.key" :aria-current="view === tab.key ? 'page' : undefined" @click="view = tab.key">
          <AppIcon :name="tab.icon" />{{ tab.label }}
        </button>
      </nav>
      <div class="topbar-tools">
        <label>
          场景
          <select :value="session.scenario" :disabled="!session.scenarios.length" aria-label="车辆场景" @change="switchScenario($event.target.value)">
            <option value="" disabled>选择场景</option>
            <option v-for="item in session.scenarios" :key="item.name" :value="item.name">{{ item.label }}</option>
          </select>
        </label>
        <button title="把车辆恢复为默认场景" @click="resetVehicle"><AppIcon name="reset" />重置车辆</button>
        <label>
          <AppIcon name="user" />
          <input :value="session.userId" aria-label="用户 ID" @change="changeUser($event.target.value)" @keydown.enter="$event.target.blur()" />
        </label>
        <button :disabled="session.busy" @click="newConversation"><AppIcon name="plus" />新对话</button>
      </div>
    </header>

    <CockpitView v-if="view === 'cockpit'" />
    <EvalView v-else-if="view === 'eval'" />
    <SystemView v-else />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import AppIcon from './components/AppIcon.vue'
import BrandMark from './components/BrandMark.vue'
import CockpitView from './views/CockpitView.vue'
import EvalView from './views/EvalView.vue'
import SystemView from './views/SystemView.vue'
import { changeUser, loadCockpit, newConversation, resetVehicle, session, switchScenario } from './lib/session'

const tabs = [
  { key: 'cockpit', label: '座舱', icon: 'car' },
  { key: 'eval', label: '评测', icon: 'chart' },
  { key: 'system', label: '系统', icon: 'system' }
]
const view = ref('cockpit')

onMounted(loadCockpit)
</script>
