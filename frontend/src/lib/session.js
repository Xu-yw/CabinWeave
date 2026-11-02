// 座舱会话状态（跨页面共享）：用户与会话 ID、对话消息、车辆状态、场景、变化高亮，以及发送、切场景等动作。
// 用户 ID 和会话 ID 存在 localStorage（读写失败时只在内存里保留）。
import { reactive } from 'vue'
import { api } from './api'

const STORAGE_KEY = 'cabinweave.frontend.settings'
const HIGHLIGHT_MS = 3000

function readSaved() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
  } catch {
    return {}
  }
}

const saved = readSaved()

export const session = reactive({
  userId: saved.userId || 'driver01',
  conversationId: saved.conversationId || '',
  messages: [],            // {id, role: 'user' | 'assistant' | 'notice', text, data?, toolCalls?, error?}
  selectedId: null,        // 链路追踪显示的那条助手回复
  vehicle: null,           // 完整车辆状态
  scenario: '',            // 本页最近一次套用的场景；刷新后不知道车辆当前对应哪个预设，留空显示"选择场景"
  scenarios: [],
  defaultScenario: 'parked',
  changed: {},             // 字段 → 高亮截止时间
  busy: false,
  loadError: ''
})

let seq = 0
const nextId = () => `m${Date.now()}-${seq++}`

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ userId: session.userId, conversationId: session.conversationId }))
  } catch {
    // 隐私模式等情况下存不了，只保留在内存里
  }
}

function highlight(changes) {
  const until = Date.now() + HIGHLIGHT_MS
  for (const change of changes || []) session.changed[change.field] = until
  setTimeout(() => {
    const now = Date.now()
    for (const [field, expire] of Object.entries(session.changed)) {
      if (expire <= now) delete session.changed[field]
    }
  }, HIGHLIGHT_MS + 50)
}

/** 某个字段（或以它开头的字段组）这一轮是否变化过 */
export function isChanged(prefix) {
  return Object.keys(session.changed).some(field => field === prefix || field.startsWith(`${prefix}.`))
}

export async function loadCockpit() {
  session.loadError = ''
  try {
    const [scenarios, vehicle] = await Promise.all([api.scenarios(), api.vehicleState(session.userId)])
    session.scenarios = scenarios.scenarios
    session.defaultScenario = scenarios.default_scenario
    session.vehicle = vehicle.state
  } catch (error) {
    session.loadError = error.message
  }
}

export async function sendMessage(text) {
  const message = text.trim()
  if (!message || session.busy) return
  session.messages.push({ id: nextId(), role: 'user', text: message })
  session.busy = true
  try {
    const data = await api.chat({
      message,
      user_id: session.userId,
      conv_id: session.conversationId || undefined
    })
    session.conversationId = data.conv_id
    persist()
    const reply = { id: nextId(), role: 'assistant', text: data.response, data, toolCalls: null }
    session.messages.push(reply)
    session.selectedId = reply.id
    session.vehicle = data.vehicle_state
    highlight(data.state_changes)
    loadToolCalls(reply)
  } catch (error) {
    session.messages.push({ id: nextId(), role: 'assistant', text: error.message, error: true })
  } finally {
    session.busy = false
  }
}

async function loadToolCalls(reply) {
  try {
    const trace = await api.toolTrace(reply.data.request_id)
    const target = session.messages.find(m => m.id === reply.id)
    if (target) target.toolCalls = trace.found ? trace.trace.tool_calls || [] : []
  } catch {
    const target = session.messages.find(m => m.id === reply.id)
    if (target) target.toolCalls = []
  }
}

async function applyVehicleResult(promise, notice) {
  try {
    const result = await promise
    session.vehicle = result.state
    session.scenario = result.scenario
    highlight(result.changes)
    session.messages.push({ id: nextId(), role: 'notice', text: notice(result) })
  } catch (error) {
    session.messages.push({ id: nextId(), role: 'notice', text: error.message, error: true })
  }
}

const scenarioLabel = name => session.scenarios.find(s => s.name === name)?.label || name

export function switchScenario(name) {
  return applyVehicleResult(api.applyScenario(session.userId, name), r => `车辆已切换到「${scenarioLabel(r.scenario)}」场景`)
}

export function resetVehicle() {
  return applyVehicleResult(api.resetVehicle(session.userId), r => `车辆已重置为「${scenarioLabel(r.scenario)}」场景`)
}

export function newConversation() {
  session.conversationId = ''
  session.messages = []
  session.selectedId = null
  persist()
}

export function changeUser(userId) {
  const value = userId.trim()
  if (!value || value === session.userId) return
  session.userId = value
  session.scenario = ''
  newConversation()
  loadCockpit()
}
