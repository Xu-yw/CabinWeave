<!-- CabinWeave 前端外框：顶栏（品牌、页面切换、场景与车辆、用户与新对话）+ 座舱 / 评测 / 系统三个页面；悬停提示的公共延时在这里统一设置。 -->
<template>
  <TooltipProvider :delay-duration="300" :skip-delay-duration="200">
    <div class="app">
      <header class="topbar">
        <div class="brand"><BrandMark />CabinWeave</div>
        <nav class="tabs" aria-label="页面">
          <button v-for="tab in tabs" :key="tab.key" :aria-current="view === tab.key ? 'page' : undefined" @click="view = tab.key">
            <AppIcon :name="tab.icon" />{{ tab.label }}
          </button>
        </nav>
        <div class="topbar-tools">
          <div class="tool"><span class="tool-label">场景</span><ScenarioSelect /></div>
          <Tip :text="resetHint">
            <button @click="resetVehicle"><AppIcon name="reset" />重置车辆</button>
          </Tip>
          <Tip text="用户 ID：每个用户有自己的车辆、对话记录和偏好">
            <label class="tool user-field">
              <AppIcon name="user" />
              <input :value="session.userId" aria-label="用户 ID" @change="changeUser($event.target.value)" @keydown.enter="$event.target.blur()" />
            </label>
          </Tip>
          <button :disabled="session.busy" @click="newConversation"><AppIcon name="plus" />新对话</button>
        </div>
      </header>

      <CockpitView v-if="view === 'cockpit'" />
      <EvalView v-else-if="view === 'eval'" />
      <SystemView v-else />
    </div>
  </TooltipProvider>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { TooltipProvider } from 'reka-ui'
import AppIcon from './components/AppIcon.vue'
import BrandMark from './components/BrandMark.vue'
import ScenarioSelect from './components/ScenarioSelect.vue'
import Tip from './components/Tip.vue'
import CockpitView from './views/CockpitView.vue'
import EvalView from './views/EvalView.vue'
import SystemView from './views/SystemView.vue'
import { changeUser, loadCockpit, newConversation, resetVehicle, session } from './lib/session'

const tabs = [
  { key: 'cockpit', label: '座舱', icon: 'car' },
  { key: 'eval', label: '评测', icon: 'chart' },
  { key: 'system', label: '系统', icon: 'system' }
]
const view = ref('cockpit')

const resetHint = computed(() => {
  const label = session.scenarios.find(s => s.name === session.defaultScenario)?.label
  return label ? `把车辆恢复为默认的「${label}」场景` : '把车辆恢复为默认场景'
})

onMounted(loadCockpit)
</script>
