<!-- 统一图标组件：按名字映射到 Lucide 图标（按需打包），线宽统一为 1.75。 -->
<template>
  <component :is="icons[name] || Dot" class="app-icon" :stroke-width="1.75" aria-hidden="true" />
</template>

<script setup>
import {
  Armchair, ArrowUp, AudioLines, Ban, BatteryLow, Building2, Car, ChartColumn, Check, ChevronDown, Dot, Fan, Gauge,
  MessageSquarePlus, Mic, MonitorCog, Music, Navigation, PanelRightClose, PanelRightOpen, RefreshCw, RotateCcw, Search,
  Snowflake, SquareParking, Thermometer, TriangleAlert, User, Volume2, VolumeX, Wind, Workflow, Zap
} from 'lucide-vue-next'

defineProps({ name: { type: String, default: 'dot' } })

const icons = {
  // 顶栏与页面
  car: Car, chart: ChartColumn, system: MonitorCog, user: User, plus: MessageSquarePlus, reset: RotateCcw,
  refresh: RefreshCw, search: Search, check: Check, chevron: ChevronDown,
  'panel-close': PanelRightClose, 'panel-open': PanelRightOpen,
  // 对话与链路
  send: ArrowUp, mic: Mic, speaking: AudioLines, 'speaker-on': Volume2, 'speaker-off': VolumeX,
  bolt: Zap, route: Workflow, block: Ban,
  // 车辆面板
  thermometer: Thermometer, fan: Fan, defrost: Wind, seat: Armchair, volume: Volume2, music: Music,
  navigation: Navigation, warning: TriangleAlert,
  // 场景预设
  parked: SquareParking, city_driving: Building2, highway: Gauge, low_battery: BatteryLow,
  tpms_warning: TriangleAlert, winter: Snowflake
}
</script>
