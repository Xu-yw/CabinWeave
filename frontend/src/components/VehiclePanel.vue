<!-- 车辆状态面板：行驶与能源、车身俯视图、空调与座椅、胎压与故障灯、导航与媒体；本轮变化的项描琥珀色。 -->
<template>
  <section class="panel vehicle">
    <div class="panel-head">
      <h2>车辆</h2>
      <span class="muted">{{ state.location?.city }}，车外 <span class="num">{{ state.outside_temp_c }}</span>℃</span>
    </div>
    <div class="panel-body">
      <div class="drive">
        <div :class="['speed', { changed: isChanged('speed_kmh') }]">
          <span class="num">{{ state.speed_kmh }}</span><small>km/h</small>
        </div>
        <div class="gears" :class="{ changed: isChanged('gear') }" aria-label="档位">
          <span v-for="g in GEARS" :key="g" :class="{ on: state.gear === g }" class="num">{{ g }}</span>
        </div>
        <div :class="['mode', { changed: isChanged('drive_mode') }]">{{ DRIVE_MODE_LABELS[state.drive_mode] || state.drive_mode }}模式</div>
      </div>

      <div :class="['energy', { low: state.battery_pct < 20, changed: isChanged('battery_pct') || isChanged('range_km') }]">
        <div class="bar"><i :style="{ width: `${state.battery_pct}%` }" /></div>
        <span><b class="num">{{ state.battery_pct }}</b>%</span>
        <span class="muted">续航 <b class="num">{{ state.range_km }}</b> km</span>
      </div>

      <CarTopView :state="state" />

      <div class="tiles">
        <div :class="['tile', { changed: isChanged('climate.on') || isChanged('climate.temp') }]">
          <span class="label">空调</span>
          <span v-if="state.climate.on" class="value"><b class="num">{{ state.climate.temp }}</b>℃</span>
          <span v-else class="value off">关闭 <small class="num">{{ state.climate.temp }}℃</small></span>
        </div>
        <div :class="['tile', { changed: isChanged('climate.fan_level') }]">
          <span class="label">风量</span><span class="value"><b class="num">{{ state.climate.fan_level }}</b> 档</span>
        </div>
        <div :class="['tile', { changed: isChanged('climate.defrost') }]">
          <span class="label">除雾</span><span :class="['value', { off: !state.climate.defrost }]">{{ state.climate.defrost ? '开启' : '关闭' }}</span>
        </div>
        <div v-for="seat in SEATS" :key="seat.key" :class="['tile', { changed: isChanged(`seats.seat_heat.${seat.key}`) }]">
          <span class="label">{{ seat.label }}座椅加热</span>
          <span :class="['value', { off: !state.seats.seat_heat[seat.key] }]">
            <template v-if="state.seats.seat_heat[seat.key]"><b class="num">{{ state.seats.seat_heat[seat.key] }}</b> 档</template>
            <template v-else>关闭</template>
          </span>
        </div>
        <div :class="['tile', { changed: isChanged('media.volume') }]">
          <span class="label">音量</span>
          <span class="value"><b class="num">{{ state.media.volume }}</b></span>
        </div>
      </div>

      <div :class="['row', { changed: isChanged('media.playing') || isChanged('media.title') }]">
        <span class="label">媒体</span>
        <span>{{ state.media.playing ? `正在播放 ${state.media.title || ''}` : state.media.title ? `已暂停 ${state.media.title}` : '未播放' }}</span>
      </div>
      <div :class="['row', { changed: isChanged('navigation') }]">
        <span class="label">导航</span>
        <span v-if="state.navigation.active">{{ state.navigation.destination }}<span class="muted">，约 {{ state.navigation.eta_min }} 分钟</span></span>
        <span v-else class="muted">未导航</span>
      </div>

      <div :class="['tires', { changed: isChanged('tire_pressure_kpa') }]" aria-label="胎压">
        <span class="label">胎压 kPa</span>
        <div class="tire-grid">
          <span v-for="w in WHEELS" :key="w" :class="['num', { low: state.tire_pressure_kpa[w] < 200 }]">{{ state.tire_pressure_kpa[w] }}</span>
        </div>
      </div>

      <div :class="['row', 'warnings', { changed: isChanged('warning_lights') }]">
        <span class="label">故障灯</span>
        <span v-if="state.warning_lights.length" class="lights">
          <em v-for="light in state.warning_lights" :key="light">{{ light }}</em>
        </span>
        <span v-else class="muted">无</span>
      </div>
    </div>
  </section>
</template>

<script setup>
import CarTopView from './CarTopView.vue'
import { DRIVE_MODE_LABELS } from '../lib/labels'
import { isChanged } from '../lib/session'

defineProps({ state: { type: Object, required: true } })

const GEARS = ['P', 'R', 'N', 'D']
const SEATS = [{ key: 'driver', label: '主驾' }, { key: 'passenger', label: '副驾' }]
const WHEELS = ['fl', 'fr', 'rl', 'rr']
</script>

<style scoped>
.drive { display: flex; align-items: flex-end; gap: 14px; }
.speed .num { font-size: 52px; line-height: 1; font-weight: 600; }
.speed small { margin-left: 4px; color: var(--ink-3); }
.gears { display: flex; gap: 6px; padding-bottom: 6px; }
.gears span { color: var(--ink-3); font-size: 18px; width: 18px; text-align: center; }
.gears span.on { color: var(--ink); font-weight: 700; }
.mode { margin-left: auto; padding-bottom: 8px; color: var(--ink-2); }

.energy { display: flex; align-items: center; gap: 10px; margin: 10px 0 4px; }
.bar { flex: 1; height: 8px; background: var(--raised); border-radius: 99px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--green); border-radius: 99px; transition: width .4s; }
.energy.low .bar i { background: var(--amber); }

.tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 4px; }
.tile { background: var(--raised); border: 1px solid transparent; border-radius: var(--r-tile); padding: 6px 9px; display: flex; flex-direction: column; }
.label { color: var(--ink-3); font-size: 12px; }
.value b { font-size: 18px; font-weight: 600; }
.value.off { color: var(--ink-3); }

.row { display: flex; gap: 10px; align-items: baseline; padding: 7px 2px 0; border-radius: 6px; }
.row .label { flex: none; width: 44px; }

.tires { display: flex; gap: 10px; align-items: center; padding: 7px 2px 0; }
.tires .label { width: 44px; line-height: 1.3; }
.tire-grid { display: grid; grid-template-columns: repeat(2, 56px); gap: 2px 10px; }
.tire-grid .low { color: var(--red); font-weight: 700; }

.lights { display: flex; flex-wrap: wrap; gap: 6px; }
.lights em { font-style: normal; color: var(--red); background: var(--red-soft); border-radius: 6px; padding: 0 8px; }

.changed { color: var(--amber); }
.tile.changed { border-color: var(--amber); background: var(--amber-soft); }
.row.changed, .tires.changed { box-shadow: inset 2px 0 0 var(--amber); padding-left: 8px; }
.gears.changed span.on { color: var(--amber); }
</style>
