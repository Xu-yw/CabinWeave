// CabinWeave 后端接口封装：地址取运行时配置 window.__CABINWEAVE_CONFIG__.pythonApiUrl（默认 /api/python，
// 由 Vite 代理或 Nginx 转发到后端）；出错时抛出带中文原因的 Error（取 FastAPI 的 detail）。

function baseUrl() {
  const runtime = typeof window !== 'undefined' ? window.__CABINWEAVE_CONFIG__ || {} : {}
  return String(runtime.pythonApiUrl || import.meta.env.VITE_PYTHON_API_URL || '/api/python').replace(/\/+$/, '')
}

async function request(path, { method = 'GET', body } = {}) {
  let response
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined
    })
  } catch {
    throw new Error('连不上后端服务，请确认后端已启动')
  }
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = data && typeof data === 'object' ? data.detail : data
    const error = new Error(typeof detail === 'string' && detail ? detail : `请求失败（${response.status}）`)
    error.status = response.status
    throw error
  }
  return data
}

const query = params => new URLSearchParams(params).toString()

export const api = {
  chat: payload => request('/chat', { method: 'POST', body: payload }),
  toolTrace: requestId => request(`/trace/tool/${encodeURIComponent(requestId)}`),
  vehicleState: userId => request(`/vehicle/state?${query({ user_id: userId })}`),
  scenarios: () => request('/vehicle/scenarios'),
  applyScenario: (userId, scenario) => request('/vehicle/scenario', { method: 'POST', body: { user_id: userId, scenario } }),
  resetVehicle: userId => request('/vehicle/reset', { method: 'POST', body: { user_id: userId } }),
  profile: userId => request(`/profile?${query({ user_id: userId })}`),
  evalReport: () => request('/eval/report'),
  monitor: () => request('/monitor'),
  skills: () => request('/skills'),
  reloadSkills: () => request('/skills/reload', { method: 'POST' }),
  knowledgeStats: () => request('/knowledge/stats'),
  searchManual: (text, topK = 3) => request(`/search?${query({ query: text, top_k: topK })}`, { method: 'POST' })
}
