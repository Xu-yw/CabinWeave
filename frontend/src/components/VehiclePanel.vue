<!-- 车辆状态面板：车速仪表、档位、驾驶模式与电量；车身俯视图（含四轮胎压）；空调与座椅等设置；媒体、导航与故障灯。
     数字变化时滚动过渡，本轮变化的项描琥珀色。 -->
<template>
  <section class="panel vehicle">
    <div class="panel-head">
      <h2>车辆</h2>
      <span class="muted">{{ state.location?.city }}，车外 <span class="num">{{ state.outside_temp_c }}</span>℃</span>
    </div>
    <div class="panel-body">
      <div class="cluster">
        <SpeedGauge :speed="state.speed_kmh" :changed="isChanged('speed_kmh')" />
        <div class="cluster-side">
          <div :class="['gears', { changed: isChanged('gear') }]" aria-label="档位">
            <span v-for="g in GEARS" :key="g" :class="{ on: state.gear === g }" class="num">{{ g }}</span>
          </div>
          <div :class="['mode', { changed: isChanged('drive_mode') }]">{{ DRIVE_MODE_LABELS[state.drive_mode] || state.drive_mode }}模式</div>
          <div :class="['energy', { low: state.battery_pct < 20, changed: isChanged('battery_pct') || isChanged('range_km') }]">
            <div class="energy-line">
              <span><b class="num"><NumberFlow :value="state.battery_pct" /></b>%</span>
              <span class="muted">续航 <span class="num"><NumberFlow :value="state.range_km" /></span> km</span>
            </div>
            <div class="bar"><i :style="{ width: `${state.battery_pct}%` }" /></div>
          </div>
        </div>
      </div>

      <CarTopView :state="state" />

      <div class="tiles">
        <div :class="['tile', { changed: isChanged('climate.on') || isChanged('climate.temp') }]">
          <span class="label"><AppIcon name="thermometer" />空调</span>
          <span v-if="state.climate.on" class="value"><b class="num"><NumberFlow :value="state.climate.temp" /></b>℃</span>
          <span v-else class="value off">关闭 <small class="num">{{ state.climate.temp }}℃</small></span>
        </div>
        <div :class="['tile', { changed: isChanged('climate.fan_level') }]">
          <span class="label"><AppIcon name="fan" />风量</span>
          <span class="value"><b class="num"><NumberFlow :value="state.climate.fan_level" /></b> 档</span>
        </div>
        <div :class="['tile', { changed: isChanged('climate.defrost') }]">
          <span class="label"><AppIcon name="defrost" />除雾</span>
          <span :class="['value', { off: !state.climate.defrost }]">{{ state.climate.defrost ? '开启' : '关闭' }}</span>
        </div>
        <div v-for="seat in SEATS" :key="seat.key" :class="['tile', { changed: isChanged(`seats.seat_heat.${seat.key}`) }]">
          <span class="label"><AppIcon name="seat" />{{ seat.label }}加热</span>
          <span :class="['value', { off: !state.seats.seat_heat[seat.key] }]">
            <template v-if="state.seats.seat_heat[seat.key]"><b class="num"><NumberFlow :value="state.seats.seat_heat[seat.key]" /></b> 档</template>
            <template v-else>关闭</template>
          </span>
        </div>
        <div :class="['tile', { changed: isChanged('media.volume') }]">
          <span class="label"><AppIcon name="volume" />音量</span>
          <span class="value"><b class="num"><NumberFlow :value="state.media.volume" /></b></span>
        </div>
      </div>

      <div class="rows">
        <div :class="['row', { changed: isChanged('media.playing') || isChanged('media.title') }]">
          <span class="label"><AppIcon name="music" />媒体</span>
          <span>{{ state.media.playing ? `正在播放 ${state.media.title || ''}` : state.media.title ? `已暂停 ${state.media.title}` : '未播放' }}</span>
        </div>
        <div :class="['row', { changed: isChanged('navigation') }]">
          <span class="label"><AppIcon name="navigation" />导航</span>
          <span v-if="state.navigation.active">{{ state.navigation.destination }}<span class="muted">，约 {{ state.navigation.eta_min }} 分钟</span></span>
          <span v-else class="muted">未导航</span>
        </div>
        <div :class="['row', 'warnings', { changed: isChanged('warning_lights') }]">
          <span class="label"><AppIcon name="warning" />故障灯</span>
          <span v-if="state.warning_lights.length" class="lights">
            <em v-for="light in state.warning_lights" :key="light">{{ light }}</em>
          </span>
          <span v-else class="muted">无</span>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import NumberFlow from '@number-flow/vue'
import AppIcon from './AppIcon.vue'
import CarTopView from './CarTopView.vue'
import SpeedGauge from './SpeedGauge.vue'
import { DRIVE_MODE_LABELS } from '../lib/labels'
import { isChanged } from '../lib/session'

defineProps({ state: { type: Object, required: true } })

const GEARS = ['P', 'R', 'N', 'D']
const SEATS = [{ key: 'driver', label: '主驾' }, { key: 'passenger', label: '副驾' }]
</script>

<style scoped>
.cluster { display: flex; align-items: center; gap: 14px; }
.cluster-side { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }

.gears { display: flex; gap: 4px; }
.gears span { color: var(--ink-3); font-size: 16px; width: 26px; height: 26px; display: grid; place-items: center; border-radius: 7px; }
.gears span.on { color: var(--ink); background: var(--raised); font-weight: 700; }
.gears.changed span.on { color: var(--amber); background: var(--amber-soft); }
.mode { color: var(--ink-2); font-size: 13px; }

.energy-line { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; font-size: 13px; }
.energy-line b { font-size: 18px; font-weight: 600; }
.bar { height: 6px; margin-top: 4px; background: var(--raised); border-radius: 99px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--green); border-radius: 99px; transition: width .6s cubic-bezier(.2, .8, .2, 1); }
.energy.low .bar i { background: var(--amber); }

.tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 4px; }
.tile { background: var(--raised); border: 1px solid transparent; border-radius: var(--r-tile); padding: 6px 9px; display: flex; flex-direction: column; transition: border-color .3s, background .3s; }
.label { color: var(--ink-3); font-size: 12px; display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.label .app-icon { width: 13px; height: 13px; }
.value b { font-size: 18px; font-weight: 600; }
.value.off { color: var(--ink-3); }

.rows { margin-top: 4px; }
.row { display: flex; gap: 10px; align-items: baseline; padding: 7px 2px 0; border-radius: 6px; }
.row .label { flex: none; width: 58px; }

.lights { display: flex; flex-wrap: wrap; gap: 6px; }
.lights em { font-style: normal; color: var(--red); background: var(--red-soft); border-radius: 6px; padding: 0 8px; }

.changed { color: var(--amber); }
.tile.changed { border-color: var(--amber); background: var(--amber-soft); }
.tile.changed .label { color: var(--amber); }
.row.changed { box-shadow: inset 2px 0 0 var(--amber); padding-left: 8px; }
</style>
