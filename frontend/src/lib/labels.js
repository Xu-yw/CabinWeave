// 界面上的中文名与取值格式：意图、Agent、快速通道规则、工具、车辆状态字段、识别决策层、场景说明等。

export const INTENT_LABELS = {
  climate_control: '空调温控',
  body_control: '车身控制',
  drive_setting: '驾驶设置',
  navigation: '导航路线',
  poi_search: '周边搜索',
  vehicle_status: '车况查询',
  fault_diagnosis: '故障诊断',
  manual_qa: '用车问答',
  media_control: '影音控制',
  phone_call: '电话消息',
  emergency_sos: '紧急求助',
  roadside_assist: '道路救援',
  chitchat: '闲聊问候',
  other: '无法识别'
}

export const AGENT_LABELS = {
  vehicle_control: '车控',
  navigation: '导航',
  advisor: '用车顾问',
  cabin: '座舱助手',
  safety: '安全节点'
}

export const FAST_RULE_LABELS = {
  F01: '空调开关', F02: '设定温度', F03: '相对温度', F04: '设定风量', F05: '相对风量', F06: '除雾',
  F07: '座椅加热开关', F08: '座椅加热档位', F09: '车窗', F10: '天窗', F11: '后备箱', F12: '锁车',
  F13: '氛围灯', F14: '双闪', F15: '播放控制', F16: '设定音量', F17: '相对音量', F18: '静音'
}

export const TOOL_LABELS = {
  set_climate: '空调', set_body: '车身', set_lighting: '灯光', set_drive_mode: '驾驶模式',
  search_poi: '搜索地点', plan_route: '规划路线', get_route_status: '路况',
  lookup_warning: '查故障灯', estimate_range: '估算续航', search_knowledge_base: '查用车手册',
  control_media: '媒体', set_volume: '音量', make_call: '电话', get_weather: '天气',
  suggest_required_fields: '追问'
}

export const DECIDED_BY_LABELS = {
  emergency_rule: '紧急短语直达',
  llm: 'LLM 判断',
  keyword_fallback: '关键词兜底',
  fast_path: '快速通道规则',
  none: '没有结果'
}

export const CONFIDENCE_LABELS = { high: '高', medium: '中', low: '低', none: '无' }

export const LLM_STATUS_LABELS = { ok: '正常', failed: '失败', timeout: '超时', skipped: '未调用' }

export const DRIVE_MODE_LABELS = { eco: '经济', comfort: '舒适', sport: '运动' }

const SEAT = { driver: '主驾', passenger: '副驾' }
const WHEEL = { fl: '左前', fr: '右前', rl: '左后', rr: '右后' }
const WINDOW = { fl: '主驾', fr: '副驾', rl: '左后', rr: '右后' }

const FIELD_LABELS = {
  speed_kmh: '车速', gear: '档位', drive_mode: '驾驶模式', battery_pct: '电量', range_km: '续航',
  'climate.on': '空调', 'climate.temp': '空调温度', 'climate.fan_level': '风量', 'climate.defrost': '除雾',
  'body.sunroof': '天窗', 'body.doors_locked': '车门', 'body.trunk_open': '后备箱',
  'lights.ambient': '氛围灯', 'lights.hazard': '双闪',
  warning_lights: '故障灯', next_service_km: '距下次保养', outside_temp_c: '车外温度',
  'navigation.active': '导航', 'navigation.destination': '目的地', 'navigation.eta_min': '预计用时',
  'media.playing': '播放', 'media.title': '曲目', 'media.volume': '音量',
  'location.city': '城市', 'location.address': '位置'
}

export function fieldLabel(field) {
  if (FIELD_LABELS[field]) return FIELD_LABELS[field]
  const [group, sub, key] = String(field).split('.')
  if (group === 'seats' && sub === 'seat_heat') return `${SEAT[key] || key}座椅加热`
  if (group === 'body' && sub === 'windows') return `${WINDOW[key] || key}车窗`
  if (group === 'tire_pressure_kpa') return `${WHEEL[sub] || sub}胎压`
  return field
}

const ON_OFF = new Set(['climate.on', 'climate.defrost', 'lights.ambient', 'lights.hazard', 'navigation.active'])

export function formatValue(field, value) {
  if (value === null || value === undefined || value === '') return '无'
  if (field === 'body.doors_locked') return value ? '已锁' : '未锁'
  if (field === 'body.trunk_open') return value ? '打开' : '关闭'
  if (field === 'media.playing') return value ? '播放中' : '暂停'
  if (ON_OFF.has(field)) return value ? '开' : '关'
  if (field === 'drive_mode') return DRIVE_MODE_LABELS[value] || value
  if (field === 'warning_lights') return value.length ? value.join('、') : '无'
  if (field === 'climate.temp') return `${value}℃`
  if (field === 'outside_temp_c') return `${value}℃`
  if (field === 'climate.fan_level' || field.startsWith('seats.')) return `${value} 档`
  if (field === 'battery_pct' || field === 'body.sunroof' || field.startsWith('body.windows.')) return `${value}%`
  if (field === 'speed_kmh') return `${value} km/h`
  if (field === 'range_km' || field === 'next_service_km') return `${value} km`
  if (field.startsWith('tire_pressure_kpa.')) return `${value} kPa`
  if (field === 'navigation.eta_min') return `${value} 分钟`
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

/** 场景预设的说明：把后端返回的 overrides 展开成"车速 110 km/h，档位 D"这样的一句话 */
export function describeScenario(overrides) {
  const flatten = (obj, prefix = '') => Object.entries(obj || {}).flatMap(([key, value]) =>
    value && typeof value === 'object' && !Array.isArray(value) ? flatten(value, `${prefix}${key}.`) : [[`${prefix}${key}`, value]])
  const items = flatten(overrides)
  return items.length ? items.map(([field, value]) => `${fieldLabel(field)} ${formatValue(field, value)}`).join('，') : '默认状态'
}

export function formatMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return '—'
  const value = Number(ms)
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  if (value < 1) return `${value.toFixed(2)} ms`
  return `${value.toFixed(value < 10 ? 1 : 0)} ms`
}

export function formatPct(value) {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}
